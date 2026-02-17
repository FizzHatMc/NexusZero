# Custom Spotify Player + Telemetry Dashboard
### Raspberry Pi Zero 2 W · PyQt5 · No Browser Engine

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  MainWindow (Qt Main Thread – UI ONLY, never blocks)            │
│                                                                 │
│  ┌─────────────────────────┐  ┌──────────────┐  ┌──────────┐   │
│  │   QStackedWidget        │  │  Skyblock    │  │  Footer  │   │
│  │  [Now Playing]          │  │  Sidebar     │  │ Printer  │   │
│  │  [Playlist View]        │  │  (QTimer     │  │ Status   │   │
│  └─────────────────────────┘  │   500ms)     │  └──────────┘   │
│                               └──────────────┘                 │
│  InputHandler (Qt signals, swappable backend)                   │
└──────────┬──────────────────────────────┬───────────────────────┘
           │ pyqtSignal (thread-safe)      │ pyqtSignal
    ┌──────▼──────┐                ┌──────▼──────┐
    │ MopidyWorker│                │Moonraker    │
    │ (QThread)   │                │Worker       │
    │ MPD / mock  │                │(QThread)    │
    │ poll 2s     │                │HTTP poll 5s │
    └─────────────┘                └─────────────┘
```

---

## Installation

### 1 – Raspberry Pi OS (Lite recommended)

```bash
sudo apt update
sudo apt install -y python3-pip python3-pyqt5 libgl1 fonts-noto
```

### 2 – Python dependencies

```bash
pip3 install -r requirements.txt
```

### 3 – Mopidy (background service)

```bash
sudo apt install -y mopidy mopidy-mpd
# Edit /etc/mopidy/mopidy.conf:
#   [mpd]
#   enabled = true
#   hostname = 127.0.0.1
#   port = 6600
sudo systemctl enable mopidy
sudo systemctl start mopidy
```

### 4 – Run (desktop/test)

```bash
python3 main.py
```

### 5 – Run on Pi framebuffer (no X11)

```bash
export QT_QPA_PLATFORM=eglfs
export QT_QPA_EGLFS_HIDECURSOR=1
python3 main.py
```

---

## Configuration

Edit the top of `main.py`:

| Variable | Default | Description |
|---|---|---|
| `PRINTER_IP` | `192.168.1.100` | Moonraker/Klipper host |
| `MOPIDY_HOST` | `localhost` | Mopidy MPD host |
| `MOPIDY_MPD_PORT` | `6600` | MPD port |
| `FREE_WILL_CYCLE_HRS` | `96` | Rift/Free Will cycle length |

---

## Key Bindings (Test Mode)

| Key | Hardware Equivalent | Action |
|---|---|---|
| `↑` / `↓` | Rotary Encoder | Scroll playlist |
| `Enter` | Encoder Click | Toggle view / select playlist |
| `1` | Button 1 | Play / Pause |
| `2` | Button 2 | Next track |
| `3` | Button 3 | Previous track |
| `4` | Button 4 | Shuffle toggle |
| `5` | Button 5 | Loop toggle |
| `S` | Button 6 | Sidebar show/hide |

---

## Migrating InputHandler to Hardware (Phase 2)

`InputHandler` in `main.py` emits named Qt signals.
To swap in hardware input, **only modify `InputHandler`**:

```python
# evdev example (add to InputHandler.run() in a QThread):
import evdev
dev = evdev.InputDevice('/dev/input/event0')
for event in dev.read_loop():
    if event.type == evdev.ecodes.EV_KEY:
        if event.value == 1:  # key down
            self._dispatch(EVDEV_KEY_MAP[event.code])
```

All other app logic stays **untouched** because it only binds to signals.

---

## Skyblock Timer Details

- **Epoch**: Unix `1560275700000` ms (11 Jun 2019 UTC)
- **Scale**: 20 real minutes = 1 SB day → 1 real second = 72 SB seconds
- **Cult of the Fallen Star**: SB days 7, 14, 21, 28 — window 00:00–06:00 SB
- **Free Will / Rift**: 96-hour real-time cycle from anchor (persisted in `.fw_anchor`)

---

## Resource Footprint (estimated, Pi Zero 2 W)

| Component | RAM |
|---|---|
| PyQt5 + app base | ~55 MB |
| Worker threads (×2) | ~3 MB |
| Album art (220×220 px) | < 1 MB |
| **Total** | **~60 MB** |

Well within the 512 MB limit.
