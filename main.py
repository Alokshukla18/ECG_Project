# ═══════════════════════════════════════════════════════════
#  ECG Monitor — ESP32 MicroPython

# ═══════════════════════════════════════════════════════════

import network
import socket
import machine
import time

# ── Config ──────────────────────────────────────────────────
SSID           = "vivo T2x 5G"
PASSWORD       = "ROHIT@7898"
SERVER_IP      = "10.241.98.15"    
SERVER_PORT    = 5005
ECG_PIN        = 34               # OUTPUT of AD8232 → GPIO34
LO_PLUS_PIN    = 33               # LO+ of AD8232   → GPIO33
LO_MINUS_PIN   = 32               # LO- of AD8232   → GPIO32
SAMPLE_RATE_HZ = 250
BATCH_SIZE     = 10
# ═══════════════════════════════════════════════════════════

# ── ADC setup ───────────────────────────────────────────────
adc = machine.ADC(machine.Pin(ECG_PIN))
adc.atten(machine.ADC.ATTN_11DB)
adc.width(machine.ADC.WIDTH_12BIT)

# ── LO+ and LO- pins (input, no pull — AD8232 drives them) ─
lo_plus  = machine.Pin(LO_PLUS_PIN,  machine.Pin.IN)
lo_minus = machine.Pin(LO_MINUS_PIN, machine.Pin.IN)

# ── WiFi ────────────────────────────────────────────────────
wlan = network.WLAN(network.STA_IF)

def connect_wifi():
    wlan.active(False)
    time.sleep(1)
    wlan.active(True)
    time.sleep(0.5)

    print("Connecting to:", SSID)
    wlan.connect(SSID, PASSWORD)

    timeout = 15
    while not wlan.isconnected() and timeout > 0:
        print(".", end="")
        time.sleep(1)
        timeout -= 1

    if wlan.isconnected():
        print("\nWiFi OK — IP:", wlan.ifconfig()[0])
        return True
    else:
        print("\nWiFi failed — check SSID and password")
        return False

# ── Socket ──────────────────────────────────────────────────
def connect_socket():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((SERVER_IP, SERVER_PORT))
        s.settimeout(None)
        print("Connected to PC server at", SERVER_IP)
        return s
    except Exception as e:
        print("Socket error:", e)
        return None

# ── Leads-off check ─────────────────────────────────────────
def leads_off():
    """
    Returns True if any electrode is detached.
    AD8232 pulls LO+ or LO- HIGH when electrode is off.
    LO+ = 1  → RA or LA electrode detached
    LO- = 1  → RL (ground) electrode detached
    """
    return lo_plus.value() == 1 or lo_minus.value() == 1

# ── First WiFi connection on boot ───────────────────────────
connected = False
for attempt in range(3):
    print("WiFi attempt", attempt + 1, "of 3")
    if connect_wifi():
        connected = True
        break
    time.sleep(2)

if not connected:
    print("Cannot connect to WiFi after 3 attempts")
    print("Check: SSID =", SSID)
    print("Check: hotspot is ON and ESP32 is in range")
    raise SystemExit

sock           = None
interval_us    = 1_000_000 // SAMPLE_RATE_HZ
batch          = []
leads_was_off  = False   

# ── Main loop ────────────────────────────────────────────────
while True:

    # Reconnect WiFi if dropped
    if not wlan.isconnected():
        print("\nWiFi lost — reconnecting...")
        sock = None
        connect_wifi()

    # Connect socket if not connected
    if sock is None:
        print("Connecting to server...")
        sock = connect_socket()
        if sock is None:
            print("Server not reachable — is pc_server.py running?")
            time.sleep(3)
            continue

    try:
        t_start = time.ticks_us()

        # ── Check electrodes before sampling ────────────────
        if leads_off():
            if not leads_was_off:
                # Print which electrode is off
                lp = lo_plus.value()
                lm = lo_minus.value()
                print("LEADS OFF —",
                      "LO+: electrode detached" if lp else "",
                      "LO-: ground detached"    if lm else "")
                leads_was_off = True

            
            batch.append("!LEADS_OFF")

        else:
            if leads_was_off:
                print("Leads reconnected — signal restored")
                leads_was_off = False

            
            val = adc.read()
            batch.append(str(val))

        if len(batch) >= BATCH_SIZE:
            sock.send(('\n'.join(batch) + '\n').encode())
            batch.clear()

        # Precise timing
        elapsed   = time.ticks_diff(time.ticks_us(), t_start)
        remaining = interval_us - elapsed
        if remaining > 0:
            time.sleep_us(remaining)

    except Exception as e:
        print("Send error:", e)
        batch.clear()
        try:
            sock.close()
        except:
            pass
        sock = None
        time.sleep(1)
