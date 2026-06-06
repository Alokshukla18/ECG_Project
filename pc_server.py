import socket
import threading
import csv
import os
import time 
import numpy as np
from collections import deque
from datetime import datetime
from scipy.signal import butter, filtfilt, find_peaks, decimate
from flask import Flask, render_template_string
from flask_socketio import SocketIO


first_emit_done = [False]

TCP_HOST       = ''            # listen on all interfaces
TCP_PORT       = 5005          # ESP32 connects here
FS             = 250           # sample rate Hz
WINDOW_SEC     = 5             # seconds of data shown on chart
SAVE_DIR       = "ecg_recordings"


BUFFER_SIZE    = FS * WINDOW_SEC
CHUNK          = 10            # process this many samples at a time

os.makedirs(SAVE_DIR, exist_ok=True)

#  Flask + SocketIO 
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ecg-secret-key'
sio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

#  Filters 
LOWCUT  = 0.5    # Hz  removes baseline wander
HIGHCUT = 40.0   # Hz  removes high frequency noise

def butter_bandpass(lowcut, highcut, fs, order=4):
    nyquist = 0.5 * fs
    low     = lowcut  / nyquist
    high    = highcut / nyquist
    b, a    = butter(order, [low, high], btype='band')
    return b, a

B_BAND, A_BAND = butter_bandpass(LOWCUT, HIGHCUT, FS)

def apply_filters(data: np.ndarray) -> np.ndarray:
    # Step 1 — remove DC offset (baseline wander)
    dc_removed = data - np.mean(data)
    # Step 2 — zero-phase bandpass filter (filtfilt = no phase distortion)
    filtered   = filtfilt(B_BAND, A_BAND, dc_removed)
    return filtered

# ── Heart rate calculation 
def calc_heart_rate(sig: np.ndarray, fs: int):
    """Returns (bpm, peaks_list, hrv_sdnn_ms)"""
    if len(sig) < fs:
        return None, [], None

    norm = sig - np.mean(sig)
    norm = norm / (np.max(np.abs(norm)) + 1e-9)

    peaks, _ = find_peaks(
        norm,
        distance=int(fs * 0.3),
        prominence=0.3,
        height=0.2
    )

    if len(peaks) < 2:
        return None, peaks.tolist(), None

    rr       = np.diff(peaks) / fs * 1000          # ms
    valid_rr = rr[(rr > 300) & (rr < 2000)]        # 30–200 BPM range

    if len(valid_rr) == 0:
        return None, peaks.tolist(), None

    bpm = round(60000 / np.mean(valid_rr), 1)
    hrv = round(float(np.std(valid_rr)), 2)         # SDNN
    return bpm, peaks.tolist(), hrv

#  Shared buffers 
raw_buf      = deque([0.0] * BUFFER_SIZE, maxlen=BUFFER_SIZE)
incoming     = deque()
inc_lock     = threading.Lock()
csv_queue    = deque()
sample_index = [0]

live_state = {
    "status":    "waiting",
    "bpm":       None,
    "hrv":       None,
    "peaks":     [],
    "samples":   0,
    "leads_off": False,   # True when any electrode is detached
}

#  CSV writer thread 
def csv_writer_thread():
    ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    csv_path = os.path.join(SAVE_DIR, f"ecg_{ts}.csv")
    print(f"[CSV]  Saving to {csv_path}")

    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['sample_index', 'time_s', 'raw_adc', 'filtered'])
        while True:
            if csv_queue:
                rows = []
                while csv_queue:
                    rows.append(csv_queue.popleft())
                writer.writerows(rows)
                f.flush()
            else:
                threading.Event().wait(0.05)

# ── Processing thread ────────────────────────────────────────
def processing_thread():
    while True:
        with inc_lock:
            n = len(incoming)
        if n < CHUNK:
            threading.Event().wait(0.02)
            continue

        with inc_lock:
            chunk = [incoming.popleft() for _ in range(min(n, CHUNK))]

        # Save raw to CSV queue
        for val in chunk:
            idx = sample_index[0]
            csv_queue.append([idx, round(idx / FS, 6), int(val), ''])
            sample_index[0] += 1

        # Update buffer + filter
        raw_buf.extend(chunk)
        raw_arr  = np.array(list(raw_buf))
        filt_arr = apply_filters(raw_arr)

        # Backfill filtered values in CSV queue
        for i, row in enumerate(list(csv_queue)[-len(chunk):]):
            row[3] = round(float(filt_arr[-(len(chunk) - i)]), 4)

        # Heart rate
        bpm, peaks, hrv = calc_heart_rate(filt_arr, FS)

        # Decimate by factor 2: 1250 → 625 points, preserves all peaks
        # decimate applies low-pass filter before downsampling — no aliasing
        raw_dec  = decimate(raw_arr,  2, zero_phase=True)
        filt_dec = decimate(filt_arr, 2, zero_phase=True)
        t_axis   = [round(i / FS - WINDOW_SEC, 3) for i in range(len(raw_arr))]
        time_dec = t_axis[::2]

        payload = {
            "raw":       raw_dec.tolist(),
            "filtered":  filt_dec.tolist(),
            "time":      time_dec,
            "bpm":       bpm,
            "hrv":       hrv,
            "peaks":     peaks,
            "samples":   sample_index[0],
            "status":    "streaming",
            "leads_off": live_state["leads_off"],
        }

        live_state.update({k: payload[k] for k in
                           ("status","bpm","hrv","peaks","samples")})

        with app.app_context():
            sio.emit('ecg_data', payload)
# ── TCP listener thread ──────────────────────────────────────
def tcp_listener_thread():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((TCP_HOST, TCP_PORT))
    sock.listen(1)
    print(f"[TCP]  Waiting for ESP32 on port {TCP_PORT}...")

    while True:
        conn, addr = sock.accept()
        print(f"[TCP]  ESP32 connected from {addr}")
        live_state["status"] = "connected"
        sio.emit('status', {"status": "connected"})

        remainder = ""
        while True:
            try:
                data = conn.recv(1024).decode('utf-8', errors='ignore')
                if not data:
                    break
                lines = (remainder + data).split('\n')
                remainder = lines[-1]
                for line in lines[:-1]:
                    line = line.strip()
                    if line == "!LEADS_OFF":
                        # Electrode detached — notify browser immediately
                        if not live_state["leads_off"]:
                            live_state["leads_off"] = True
                            sio.emit('leads_off', {"leads_off": True})
                            print("[ECG]  Leads off — electrode detached")
                    else:
                        # Electrode back on — clear the flag
                        if live_state["leads_off"]:
                            live_state["leads_off"] = False
                            sio.emit('leads_off', {"leads_off": False})
                            print("[ECG]  Leads reconnected")
                        try:
                            with inc_lock:
                                incoming.append(float(line))
                        except ValueError:
                            pass
            except Exception as e:
                print(f"[TCP]  Error: {e}")
                break

        print("[TCP]  ESP32 disconnected — waiting for reconnect...")
        live_state["status"] = "disconnected"
        sio.emit('status', {"status": "disconnected"})

# ── Flask route — serves the dashboard HTML ──────────────────
@app.route('/')
def dashboard():
    return render_template_string(open('index.html', encoding='utf-8').read())

# ── SocketIO: send current state when browser connects ──────
@sio.on('connect')
def on_connect():
    print("[WS]   Browser connected")
    sio.emit('status', {"status": live_state["status"]})

# ── Start background threads then run server ─────────────────
if __name__ == '__main__':
    threading.Thread(target=tcp_listener_thread, daemon=True).start()
    threading.Thread(target=processing_thread,   daemon=True).start()
    threading.Thread(target=csv_writer_thread,   daemon=True).start()

    print("[WEB]  Dashboard → http://localhost:5000")
    sio.run(app, host='0.0.0.0', port=5000, debug=False)
