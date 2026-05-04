import lgpio
lgpio._chip = lgpio.gpiochip_open(4)

import time
import datetime
import psutil
import socket
import subprocess
import threading
import requests
import textwrap
import select
from luma.core.interface.serial import spi
from luma.lcd.device import st7735
from luma.core.render import canvas
from evdev import InputDevice, ecodes, list_devices

# --- CONFIGURATION ---
API_KEY = "85a3f31e454468a41cef0e1a05bdd76f"
CITY = "Ames,US" 
serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
device = st7735(serial, width=160, height=128, rotate=1)
_weather_cache = None
_weather_last_fetch = 0

MODE = "DASHBOARD"

cmd_buffer = ""
terminal_history = ["Shell Ready."]
CHAR_LIMIT = 25
VISIBLE_LINES = 9
scroll_offset = 0
kbd_connected = False

# Keyboard Mapping
key_map = {
    ecodes.KEY_A: "a", ecodes.KEY_B: "b", ecodes.KEY_C: "c", ecodes.KEY_D: "d", ecodes.KEY_E: "e",
    ecodes.KEY_F: "f", ecodes.KEY_G: "g", ecodes.KEY_H: "h", ecodes.KEY_I: "i", ecodes.KEY_J: "j",
    ecodes.KEY_K: "k", ecodes.KEY_L: "l", ecodes.KEY_M: "m", ecodes.KEY_N: "n", ecodes.KEY_O: "o",
    ecodes.KEY_P: "p", ecodes.KEY_Q: "q", ecodes.KEY_R: "r", ecodes.KEY_S: "s", ecodes.KEY_T: "t",
    ecodes.KEY_U: "u", ecodes.KEY_V: "v", ecodes.KEY_W: "w", ecodes.KEY_X: "x", ecodes.KEY_Y: "y",
    ecodes.KEY_Z: "z", ecodes.KEY_1: "1", ecodes.KEY_2: "2", ecodes.KEY_3: "3", ecodes.KEY_4: "4",
    ecodes.KEY_5: "5", ecodes.KEY_6: "6", ecodes.KEY_7: "7", ecodes.KEY_8: "8", ecodes.KEY_9: "9",
    ecodes.KEY_0: "0", ecodes.KEY_SPACE: " ", ecodes.KEY_DOT: ".", ecodes.KEY_SLASH: "/",
    ecodes.KEY_MINUS: "-", ecodes.KEY_ENTER: "ENTER", ecodes.KEY_BACKSPACE: "BACK", ecodes.KEY_UP: "UP",
    ecodes.KEY_DOWN: "DOWN"
}

def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except: return "No Link"

def pi_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = int(f.read()) / 1000
            return int(temp)
    except:
        return -1

def fan_status():
    try:
        with open("/sys/class/thermal/cooling_device0/cur_state", "r") as f:
            state = f.read().strip()
            return state if state != "0" else "Off"
    except:
        return "?"

def get_weather():
    global _weather_cache, _weather_last_fetch
    if _weather_cache is not None and (time.time() - _weather_last_fetch) < 300:
        return _weather_cache
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={CITY}&appid={API_KEY}&units=metric&cnt=1"
        response = requests.get(url, timeout=5)
        
        if response.status_code != 200:
            _weather_cache = {"err": f"API {response.status_code}"}
            _weather_last_fetch = time.time()
            return _weather_cache
            
        data = response.json()['list'][0]
        
        return {
            "feels": int(data['main']['feels_like']),
            "desc": data['weather'][0]['main'],
            "wind": int(data['wind']['speed'] * 3.6), # m/s to km/h
            "rain": int(data.get('pop', 0) * 100)      # 'pop' is 0.0 to 1.0, so * 100
        }
    except Exception as e:
        _weather_cache = {"err": "Conn Error"}
        _weather_last_fetch = time.time()
        return _weather_cache

def execute_cmd(command):
    global terminal_history
    if len(terminal_history) > 500:
        terminal_history = terminal_history[300:]
    try:
        res = subprocess.check_output(command, shell=True, stderr=subprocess.STDOUT, timeout=5).decode('utf-8')
        for line in res.split('\n'):
            wrapped = textwrap.wrap(line, width=CHAR_LIMIT)
            if wrapped:
                terminal_history.extend(wrapped)
            elif line.strip() == "":
                terminal_history.append("")
    except Exception as e:
        terminal_history.extend(textwrap.wrap(f"Error: {str(e)}", width=CHAR_LIMIT))

def find_keyboard():
    candidates = []

    for path in list_devices():
        try:
            dev = InputDevice(path)

            bad_names = ["pwr_button", "vc4-hdmi"]
            if any(bad in dev.name.lower() for bad in bad_names):
                continue

            caps = dev.capabilities()
            if ecodes.EV_KEY not in caps:
                continue

            keys = caps[ecodes.EV_KEY]

            required_keys = [
                ecodes.KEY_A,
                ecodes.KEY_Z,
                ecodes.KEY_ENTER,
                ecodes.KEY_BACKSPACE,
                ecodes.KEY_SPACE,
            ]

            if all(k in keys for k in required_keys):
                candidates.append((path, dev))

        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda item: int(item[0].replace("/dev/input/event", "")))
    return candidates[0][1]

def keyboard_worker():
    global MODE, cmd_buffer, scroll_offset, kbd_connected

    while True:
        kbd = find_keyboard()

        if kbd is None:
            if kbd_connected:
                kbd_connected = False
                MODE = "DASHBOARD"
            time.sleep(1)
            continue

        kbd_path = kbd.path

        if not kbd_connected:
            kbd_connected = True
            MODE = "TERMINAL"

        grabbed = False

        try:
            kbd.grab()
            grabbed = True
        except Exception:
            pass

        try:
            while True:
                # If the keyboard device disappeared, go back to dashboard
                if kbd_path not in list_devices():
                    kbd_connected = False
                    MODE = "DASHBOARD"
                    break

                # Wait up to 0.5s for a key event, then re-check connection
                r, _, _ = select.select([kbd.fd], [], [], 0.5)

                if not r:
                    continue

                for event in kbd.read():
                    if event.type != ecodes.EV_KEY:
                        continue

                    if event.value not in (1, 2):
                        continue

                    if event.code == ecodes.KEY_F12:
                        MODE = "DASHBOARD" if MODE == "TERMINAL" else "TERMINAL"
                        continue

                    if MODE != "TERMINAL":
                        continue

                    if event.code not in key_map:
                        continue

                    v = key_map[event.code]

                    if v == "UP":
                        scroll_offset = min(scroll_offset + 1, max(0, len(terminal_history) - 11))

                    elif v == "DOWN":
                        scroll_offset = max(scroll_offset - 1, 0)

                    elif v == "ENTER":
                        if cmd_buffer.strip():
                            terminal_history.append(f"> {cmd_buffer}")
                            execute_cmd(cmd_buffer)
                            cmd_buffer = ""
                            scroll_offset = 0

                    elif v == "BACK":
                        cmd_buffer = cmd_buffer[:-1]

                    else:
                        if len(cmd_buffer) < 40:
                            cmd_buffer += v

        except OSError:
            kbd_connected = False
            MODE = "DASHBOARD"

        except Exception:
            kbd_connected = False
            MODE = "DASHBOARD"

        finally:
            if grabbed:
                try:
                    kbd.ungrab()
                except Exception:
                    pass

        time.sleep(1)

# Start the thread
threading.Thread(target=keyboard_worker, daemon=True).start()

# --- MAIN LOOP ---
while True:
    w = get_weather()
    with canvas(device) as draw:
        if MODE == "DASHBOARD":
            # Network & Time
            draw.text((5, 5), f"IP: {get_ip()}", fill="white")
                        
            # Stats
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
            draw.text((5, 17), f"CPU: {cpu}% | RAM: {ram}%", fill="magenta")
            try:
                disk_path = "/srv/dev-disk-by-uuid-c7802141-0553-47df-b5dc-0ee92cc42130"
                disk = psutil.disk_usage(disk_path)
                used_gb = int(disk.used / (1024**3))
                total_gb = int(disk.total / (1024**3))
                draw.text((5, 29), f"DISK: {used_gb} / {total_gb} GB", fill="orange")
            except:
                draw.text((5, 29), "DISK: Drive Not Found", fill="red")
            current_t = pi_temp()
            draw.text((5, 41), f"Temp: {current_t}C | Fan: {fan_status()}", fill="red" if current_t > 65 else "green")

            # Weather
            if w and "err" not in w:
                draw.text((5, 65), f"Feels: {w['feels']}°C | {w['desc']}", fill="cyan")
                draw.text((5, 77), f"Wind: {w['wind']} km/h", fill="white")
                draw.text((5, 89), f"Rain Chance: {w['rain']}%", fill="red")
           
            else:
                msg = w["err"] if w else "Loading..."
                draw.text((5, 65), msg, fill="red")
        else:
            end = len(terminal_history) - scroll_offset
            lines = terminal_history[max(0, end - 14):end]
            y_pos = 0
            for l in lines:
                draw.text((2, y_pos), l, fill="white")
                y_pos += 11
            
            # Draw the input bar at the absolute bottom (Y=145+)
            draw.rectangle((0, 145, 128, 160), fill="blue")
            inp_text = f"> {cmd_buffer}"
            if len(inp_text) > 21: inp_text = inp_text[-20:]
            draw.text((2, 147), f"{inp_text}_", fill="white")

    time.sleep(0.05 if MODE == "TERMINAL" else 3)
