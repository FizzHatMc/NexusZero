#!/usr/bin/env python3
"""
Custom Spotify Player + Telemetry Dashboard
Raspberry Pi Zero 2 W – PyQt5, resource-lean, no browser engine.
"""

import sys
import os
import math
import time
import json
import logging
from datetime import datetime, timezone

# ── Qt imports ───────────────────────────────────────────────────────────────
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QProgressBar,
    QStackedWidget, QFrame, QSizePolicy, QScrollArea,
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QPropertyAnimation,
    QEasingCurve, QRect, pyqtProperty, QObject,
)
from PyQt5.QtGui import (
    QFont, QFontDatabase, QColor, QPalette, QPixmap, QPainter,
    QLinearGradient, QBrush, QRadialGradient, QPen, QIcon,
)

# ── Optional deps (graceful fallback) ────────────────────────────────────────
try:
    import mpd
    MPD_AVAILABLE = True
except ImportError:
    MPD_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("CustomPlayer")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

PRINTER_IP      = "192.168.1.100"   # ← replace with your Klipper/Moonraker IP
MOPIDY_HOST     = "localhost"
MOPIDY_MPD_PORT = 6600
POLL_INTERVAL_MS_SPOTIFY  = 2000
POLL_INTERVAL_MS_PRINTER  = 5000
POLL_INTERVAL_MS_SKYBLOCK = 500

# Skyblock epoch: 11 June 2019 UTC (ms)
SB_EPOCH_MS          = 1560275700000
SB_REAL_MIN_PER_DAY  = 20          # 20 real minutes = 1 SB day
SB_DAYS_PER_MONTH    = 31
SB_HOURS_PER_DAY     = 24
FREE_WILL_CYCLE_HRS  = 96          # configurable

# ═══════════════════════════════════════════════════════════════════════════════
# COLOUR PALETTE  (dark, Spotify-inspired but more industrial/refined)
# ═══════════════════════════════════════════════════════════════════════════════

C = {
    "bg":           "#0a0a0f",
    "bg_card":      "#12121a",
    "bg_elevated":  "#1a1a26",
    "accent":       "#1db954",      # Spotify green
    "accent2":      "#e8a020",      # amber – printer / warnings
    "accent3":      "#3d8ef0",      # blue – skyblock
    "text_primary": "#e8e8f0",
    "text_secondary":"#7878a0",
    "text_dim":     "#404060",
    "border":       "#252535",
    "progress_bg":  "#202030",
    "hot":          "#ff4444",
    "cold":         "#44aaff",
}

# ═══════════════════════════════════════════════════════════════════════════════
# MOCK / FALLBACK DATA
# ═══════════════════════════════════════════════════════════════════════════════

MOCK_TRACK = {
    "title":    "Mock Song – Connect Mopidy",
    "artist":   "Unknown Artist",
    "album":    "Unknown Album",
    "duration": 210,
    "elapsed":  0,
    "state":    "stop",
    "shuffle":  False,
    "repeat":   False,
}

MOCK_PLAYLISTS = [
    "🎵 Chill Vibes",
    "🔥 Workout Beats",
    "🌙 Late Night Lo-Fi",
    "🎸 Rock Classics",
    "🎹 Piano Sessions",
    "🌿 Nature Sounds",
    "🚀 Synthwave Drive",
]

MOCK_PRINTER = {
    "state":        "standby",
    "progress":     0.0,
    "hotend_temp":  0.0,
    "hotend_target":0.0,
    "bed_temp":     0.0,
    "bed_target":   0.0,
}

# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLER  – abstraction layer, swap keyboard→evdev/gpiozero later
# ═══════════════════════════════════════════════════════════════════════════════

class InputHandler(QObject):
    """
    Translates raw input events into semantic signals.

    Current backend: Qt Key Events (keyboard simulation).
    To switch to hardware (evdev / gpiozero), subclass or replace
    _handle_raw_event() – the signals below never change.

    Key mapping (test mode):
        ↑ / ↓     → encoder_up / encoder_down   (scroll)
        Enter     → encoder_click                (select / toggle view)
        1         → btn_play_pause
        2         → btn_next
        3         → btn_prev
        4         → btn_shuffle
        5         → btn_loop
        S         → btn_sidebar_toggle
    """

    # ── Rotary Encoder ────────────────────────────────────────────────────────
    encoder_up    = pyqtSignal()
    encoder_down  = pyqtSignal()
    encoder_click = pyqtSignal()

    # ── Hardware Buttons ──────────────────────────────────────────────────────
    btn_play_pause      = pyqtSignal()
    btn_next            = pyqtSignal()
    btn_prev            = pyqtSignal()
    btn_shuffle         = pyqtSignal()
    btn_loop            = pyqtSignal()
    btn_sidebar_toggle  = pyqtSignal()

    # Key → signal map (Qt.Key → method name)
    KEY_MAP = {
        Qt.Key_Up:    "encoder_up",
        Qt.Key_Down:  "encoder_down",
        Qt.Key_Return:"encoder_click",
        Qt.Key_1:     "btn_play_pause",
        Qt.Key_2:     "btn_next",
        Qt.Key_3:     "btn_prev",
        Qt.Key_4:     "btn_shuffle",
        Qt.Key_5:     "btn_loop",
        Qt.Key_S:     "btn_sidebar_toggle",
    }

    def handle_key_event(self, key: int):
        """Called by the main window's keyPressEvent."""
        signal_name = self.KEY_MAP.get(key)
        if signal_name:
            getattr(self, signal_name).emit()
            return True
        return False

    # ── evdev / gpiozero stub ─────────────────────────────────────────────────
    # When you move to hardware, start a QThread here that reads from
    # /dev/input/eventX via evdev and calls _dispatch() accordingly.
    def _dispatch(self, signal_name: str):
        """Generic dispatcher – hardware backends call this."""
        if hasattr(self, signal_name):
            getattr(self, signal_name).emit()


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER: Mopidy / MPD
# ═══════════════════════════════════════════════════════════════════════════════

class MopidyWorker(QThread):
    track_updated    = pyqtSignal(dict)
    playlists_updated= pyqtSignal(list)
    error            = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running  = True
        self._client   = None
        self._connected= False

    # ── Public control methods (called from UI thread) ────────────────────────
    def play_pause(self):   self._safe_cmd("play_pause")
    def next_track(self):   self._safe_cmd("next")
    def prev_track(self):   self._safe_cmd("previous")
    def shuffle(self, on):  self._safe_cmd("random", on)
    def loop(self, on):     self._safe_cmd("repeat",  on)
    def play_playlist(self, name): self._safe_cmd("load_playlist", name)

    def _safe_cmd(self, cmd, *args):
        if not self._connected:
            return
        try:
            if cmd == "play_pause":
                status = self._client.status()
                if status.get("state") == "play":
                    self._client.pause(1)
                else:
                    self._client.play()
            elif cmd == "next":
                self._client.next()
            elif cmd == "previous":
                self._client.previous()
            elif cmd == "random":
                self._client.random(1 if args[0] else 0)
            elif cmd == "repeat":
                self._client.repeat(1 if args[0] else 0)
            elif cmd == "load_playlist":
                self._client.clear()
                self._client.load(args[0])
                self._client.play()
        except Exception as e:
            log.warning(f"MPD cmd {cmd} failed: {e}")
            self._connected = False

    # ── Thread loop ───────────────────────────────────────────────────────────
    def run(self):
        while self._running:
            if not self._connected:
                self._try_connect()
            if self._connected:
                self._poll()
            time.sleep(POLL_INTERVAL_MS_SPOTIFY / 1000)

    def _try_connect(self):
        if not MPD_AVAILABLE:
            self.track_updated.emit(MOCK_TRACK.copy())
            self.playlists_updated.emit(MOCK_PLAYLISTS.copy())
            return
        try:
            self._client = mpd.MPDClient()
            self._client.timeout = 3
            self._client.connect(MOPIDY_HOST, MOPIDY_MPD_PORT)
            self._connected = True
            log.info("MPD connected")
        except Exception as e:
            log.debug(f"MPD connect failed: {e}")
            self.track_updated.emit(MOCK_TRACK.copy())
            self.playlists_updated.emit(MOCK_PLAYLISTS.copy())

    def _poll(self):
        try:
            status   = self._client.status()
            currentsong = self._client.currentsong()
            playlists   = [p["playlist"] for p in self._client.listplaylists()]

            track = {
                "title":    currentsong.get("title", "Unknown"),
                "artist":   currentsong.get("artist", "Unknown"),
                "album":    currentsong.get("album",  ""),
                "duration": float(status.get("duration", 0) or 0),
                "elapsed":  float(status.get("elapsed",  0) or 0),
                "state":    status.get("state", "stop"),
                "shuffle":  status.get("random", "0") == "1",
                "repeat":   status.get("repeat",  "0") == "1",
            }
            self.track_updated.emit(track)
            self.playlists_updated.emit(playlists)
        except Exception as e:
            log.warning(f"MPD poll error: {e}")
            self._connected = False

    def stop(self):
        self._running = False
        if self._client and self._connected:
            try: self._client.close()
            except: pass


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER: Moonraker / Klipper
# ═══════════════════════════════════════════════════════════════════════════════

class MoonrakerWorker(QThread):
    printer_updated = pyqtSignal(dict)

    ENDPOINT = (
        "http://{ip}:7125/printer/objects/query"
        "?print_stats&extruder=target,temperature&heater_bed=target,temperature"
    )

    def __init__(self):
        super().__init__()
        self._running = True

    def run(self):
        while self._running:
            self._poll()
            time.sleep(POLL_INTERVAL_MS_PRINTER / 1000)

    def _poll(self):
        if not REQUESTS_AVAILABLE:
            self.printer_updated.emit(MOCK_PRINTER.copy())
            return
        url = self.ENDPOINT.format(ip=PRINTER_IP)
        try:
            r = requests.get(url, timeout=3)
            r.raise_for_status()
            data   = r.json()["result"]["status"]
            stats  = data.get("print_stats", {})
            ext    = data.get("extruder",    {})
            bed    = data.get("heater_bed",  {})
            result = {
                "state":         stats.get("state", "standby"),
                "progress":      stats.get("progress", 0.0),
                "hotend_temp":   ext.get("temperature", 0.0),
                "hotend_target": ext.get("target",      0.0),
                "bed_temp":      bed.get("temperature", 0.0),
                "bed_target":    bed.get("target",      0.0),
            }
            self.printer_updated.emit(result)
        except Exception as e:
            log.debug(f"Moonraker poll: {e}")
            self.printer_updated.emit(MOCK_PRINTER.copy())

    def stop(self):
        self._running = False


# ═══════════════════════════════════════════════════════════════════════════════
# SKYBLOCK TIMER LOGIC  (pure math, no network)
# ═══════════════════════════════════════════════════════════════════════════════

class SkyblockTimers:
    """All calculations happen here, no Qt dependency."""

    REAL_SEC_PER_SB_DAY  = SB_REAL_MIN_PER_DAY * 60          # 1200 s
    REAL_SEC_PER_SB_MONTH= REAL_SEC_PER_SB_DAY * SB_DAYS_PER_MONTH  # 37200 s
    SB_SEC_PER_SB_DAY    = SB_HOURS_PER_DAY * 3600            # 86400 SB-sec

    @classmethod
    def real_to_sb(cls, real_epoch_s: float) -> dict:
        """Convert real Unix seconds → Skyblock date/time."""
        elapsed_real   = real_epoch_s - SB_EPOCH_MS / 1000
        if elapsed_real < 0:
            elapsed_real = 0
        # How many SB-seconds have elapsed (scaled)
        sb_seconds_elapsed = elapsed_real * cls.SB_SEC_PER_SB_DAY / cls.REAL_SEC_PER_SB_DAY
        sb_total_days  = int(sb_seconds_elapsed // cls.SB_SEC_PER_SB_DAY)
        sb_time_of_day = sb_seconds_elapsed % cls.SB_SEC_PER_SB_DAY   # 0..86399 SB-sec

        sb_month = (sb_total_days // SB_DAYS_PER_MONTH) + 1
        sb_day   = (sb_total_days %  SB_DAYS_PER_MONTH) + 1
        sb_hour  = int(sb_time_of_day // 3600)
        sb_min   = int((sb_time_of_day % 3600) // 60)

        return {
            "month": sb_month, "day": sb_day,
            "hour": sb_hour, "min": sb_min,
            "sb_seconds_elapsed": sb_seconds_elapsed,
            "sb_time_of_day": sb_time_of_day,
            "real_elapsed": elapsed_real,
        }

    @classmethod
    def next_cult_event(cls, real_now_s: float) -> float:
        """
        Return real seconds until the next 'Cult of the Fallen Star'.
        Event: SB day 7, 14, 21, 28  –  00:00 to 06:00 SB time.
        We aim for the 00:00 window start.
        """
        sb = cls.real_to_sb(real_now_s)
        elapsed_real = sb["real_elapsed"]

        # Current position within one SB-month (real seconds)
        real_in_month  = elapsed_real % cls.REAL_SEC_PER_SB_MONTH
        real_per_day   = cls.REAL_SEC_PER_SB_DAY

        # Find next cult day (7,14,21,28) at SB 00:00
        event_days = [6, 13, 20, 27]   # 0-indexed within month
        best = None
        for d in event_days:
            day_start = d * real_per_day
            if day_start > real_in_month:
                gap = day_start - real_in_month
                if best is None or gap < best:
                    best = gap
        if best is None:
            # Wrap to next month
            best = cls.REAL_SEC_PER_SB_MONTH - real_in_month + event_days[0] * real_per_day

        return best   # real seconds remaining

    @classmethod
    def free_will_remaining(cls, real_now_s: float, anchor_s: float) -> float:
        """
        Relative 96-hour cycle from a given anchor timestamp.
        Returns remaining seconds in the current cycle.
        """
        cycle_s = FREE_WILL_CYCLE_HRS * 3600
        elapsed = (real_now_s - anchor_s) % cycle_s
        return cycle_s - elapsed


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS: album-art placeholder
# ═══════════════════════════════════════════════════════════════════════════════

def make_placeholder_cover(size=220) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(QColor(C["bg_elevated"]))
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)

    # Gradient circle
    grad = QRadialGradient(size/2, size/2, size/2)
    grad.setColorAt(0.0, QColor("#1db95440"))
    grad.setColorAt(1.0, QColor("#00000000"))
    painter.setBrush(QBrush(grad))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(10, 10, size-20, size-20)

    # Music note icon (simple)
    painter.setPen(QPen(QColor(C["accent"]), 3))
    painter.setFont(QFont("serif", size//4))
    painter.drawText(pix.rect(), Qt.AlignCenter, "♫")
    painter.end()
    return pix


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60:02d}:{s%60:02d}"


def fmt_countdown(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


# ═══════════════════════════════════════════════════════════════════════════════
# UI COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

def label(text="", color=None, size=11, bold=False, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    c   = color or C["text_primary"]
    w   = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color: {c}; font-size: {size}px; font-weight: {w}; background: transparent;")
    return lbl


class SeparatorLine(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setStyleSheet(f"background: {C['border']}; max-height: 1px; border: none;")


class AnimatedProgressBar(QWidget):
    """Custom progress bar with glow effect."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self.setFixedHeight(4)
        self.setStyleSheet("background: transparent;")

    def setValue(self, v: float):
        self._value = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, event):
        w = self.width()
        h = self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Track
        p.setBrush(QColor(C["progress_bg"]))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        # Fill
        fill_w = int(w * self._value)
        if fill_w > 0:
            grad = QLinearGradient(0, 0, fill_w, 0)
            grad.setColorAt(0,   QColor(C["accent"]))
            grad.setColorAt(1.0, QColor("#3dffa0"))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(0, 0, fill_w, h, 2, 2)
        p.end()


# ─── Now Playing View ─────────────────────────────────────────────────────────

class NowPlayingView(QWidget):
    play_pause_requested = pyqtSignal()
    next_requested       = pyqtSignal()
    prev_requested       = pyqtSignal()
    shuffle_requested    = pyqtSignal()
    loop_requested       = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shuffle = False
        self._loop    = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(14)

        # Album art
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(220, 220)
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setStyleSheet(
            f"border-radius: 12px; background: {C['bg_elevated']};"
            "border: 1px solid #252535;"
        )
        cover_px = make_placeholder_cover(220)
        self.cover_label.setPixmap(cover_px)

        cover_row = QHBoxLayout()
        cover_row.addStretch()
        cover_row.addWidget(self.cover_label)
        cover_row.addStretch()
        layout.addLayout(cover_row)

        # Song info
        self.title_lbl  = label("— Not Connected —", C["text_primary"], 16, bold=True)
        self.artist_lbl = label("Unknown Artist",    C["accent"],       12)
        self.album_lbl  = label("",                  C["text_secondary"], 10)
        self.title_lbl.setAlignment(Qt.AlignCenter)
        self.artist_lbl.setAlignment(Qt.AlignCenter)
        self.album_lbl.setAlignment(Qt.AlignCenter)
        self.title_lbl.setWordWrap(True)
        layout.addWidget(self.title_lbl)
        layout.addWidget(self.artist_lbl)
        layout.addWidget(self.album_lbl)

        # Progress
        prog_row = QHBoxLayout()
        prog_row.setSpacing(8)
        self.elapsed_lbl  = label("0:00", C["text_dim"], 9)
        self.duration_lbl = label("0:00", C["text_dim"], 9)
        self.progress_bar = AnimatedProgressBar()
        prog_row.addWidget(self.elapsed_lbl)
        prog_row.addWidget(self.progress_bar, 1)
        prog_row.addWidget(self.duration_lbl)
        layout.addLayout(prog_row)

        # Control buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        def ctrl_btn(symbol, tip, slot, accent=False):
            b = QPushButton(symbol)
            col = C["accent"] if accent else C["text_secondary"]
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {col}; background: transparent;
                    border: 1px solid {C['border']}; border-radius: 20px;
                    font-size: 18px; min-width: 40px; min-height: 40px;
                    max-width: 40px; max-height: 40px;
                }}
                QPushButton:hover {{ background: {C['bg_elevated']}; color: {C['accent']}; }}
                QPushButton:pressed {{ background: {C['accent']}20; }}
            """)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            return b

        self.shuffle_btn = ctrl_btn("⇌", "Shuffle [4]", self._on_shuffle)
        self.prev_btn    = ctrl_btn("⏮", "Prev [3]",    self.prev_requested.emit)
        self.play_btn    = ctrl_btn("▶", "Play/Pause [1]", self.play_pause_requested.emit, accent=True)
        self.play_btn.setStyleSheet(self.play_btn.styleSheet().replace("min-width: 40px; min-height: 40px; max-width: 40px; max-height: 40px", "min-width:48px; min-height:48px; max-width:48px; max-height:48px"))
        self.next_btn    = ctrl_btn("⏭", "Next [2]",    self.next_requested.emit)
        self.loop_btn    = ctrl_btn("↻", "Loop [5]",    self._on_loop)

        for b in [self.shuffle_btn, self.prev_btn, self.play_btn, self.next_btn, self.loop_btn]:
            btn_row.addStretch(1)
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        layout.addStretch()

    def _on_shuffle(self):
        self._shuffle = not self._shuffle
        col = C["accent"] if self._shuffle else C["text_secondary"]
        self.shuffle_btn.setStyleSheet(self.shuffle_btn.styleSheet().replace(
            self.shuffle_btn.styleSheet().split("color:")[1].split(";")[0],
            f" {col}"
        ))
        self.shuffle_requested.emit()

    def _on_loop(self):
        self._loop = not self._loop
        col = C["accent"] if self._loop else C["text_secondary"]
        self.loop_btn.setStyleSheet(self.loop_btn.styleSheet().replace(
            self.loop_btn.styleSheet().split("color:")[1].split(";")[0],
            f" {col}"
        ))
        self.loop_requested.emit()

    def update_track(self, track: dict):
        self.title_lbl.setText(track.get("title", "—"))
        self.artist_lbl.setText(track.get("artist", ""))
        self.album_lbl.setText(track.get("album", ""))
        elapsed  = track.get("elapsed",  0)
        duration = track.get("duration", 0)
        self.elapsed_lbl.setText(fmt_time(elapsed))
        self.duration_lbl.setText(fmt_time(duration))
        self.progress_bar.setValue(elapsed / duration if duration else 0)
        state = track.get("state", "stop")
        self.play_btn.setText("⏸" if state == "play" else "▶")

        self._shuffle = track.get("shuffle", False)
        self._loop    = track.get("repeat",  False)


# ─── Playlist View ────────────────────────────────────────────────────────────

class PlaylistView(QWidget):
    playlist_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 8)
        layout.setSpacing(8)

        hdr = label("PLAYLISTS", C["accent"], 11, bold=True)
        hdr.setStyleSheet(hdr.styleSheet() + "letter-spacing: 2px;")
        layout.addWidget(hdr)

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(f"""
            QListWidget {{
                background: transparent; border: none;
                color: {C['text_primary']}; font-size: 13px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 10px 8px; border-radius: 6px;
                border-bottom: 1px solid {C['border']};
            }}
            QListWidget::item:selected {{
                background: {C['accent']}22; color: {C['accent']};
                border: 1px solid {C['accent']}44;
            }}
            QListWidget::item:hover {{
                background: {C['bg_elevated']};
            }}
        """)
        self.list_widget.itemActivated.connect(lambda item: self.playlist_selected.emit(item.text()))
        layout.addWidget(self.list_widget)

        hint = label("↑/↓ scroll  •  Enter select  •  Enter toggle view", C["text_dim"], 9)
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

    def update_playlists(self, playlists: list):
        current = self.list_widget.currentRow()
        self.list_widget.clear()
        for pl in playlists:
            item = QListWidgetItem(pl)
            self.list_widget.addItem(item)
        if current >= 0 and current < self.list_widget.count():
            self.list_widget.setCurrentRow(current)
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def scroll_up(self):
        r = self.list_widget.currentRow()
        if r > 0:
            self.list_widget.setCurrentRow(r - 1)

    def scroll_down(self):
        r = self.list_widget.currentRow()
        if r < self.list_widget.count() - 1:
            self.list_widget.setCurrentRow(r + 1)

    def activate_current(self):
        item = self.list_widget.currentItem()
        if item:
            self.playlist_selected.emit(item.text())


# ─── Printer Footer ───────────────────────────────────────────────────────────

class PrinterFooter(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(48)
        self.setStyleSheet(f"background: {C['bg_card']}; border-top: 1px solid {C['border']};")

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(24)

        printer_icon = label("🖨", size=14)
        row.addWidget(printer_icon)

        self.state_lbl      = label("STANDBY",   C["text_dim"],       10, bold=True)
        self.hotend_lbl     = label("🔥 0° / 0°", C["text_secondary"], 10)
        self.bed_lbl        = label("🛏 0° / 0°", C["text_secondary"], 10)
        self.progress_lbl   = label("0%",         C["accent2"],        10)
        self.progress_mini  = AnimatedProgressBar()
        self.progress_mini.setFixedWidth(80)

        row.addWidget(self.state_lbl)
        row.addWidget(self.hotend_lbl)
        row.addWidget(self.bed_lbl)
        row.addStretch()
        row.addWidget(self.progress_lbl)
        row.addWidget(self.progress_mini)

    def update_printer(self, data: dict):
        state = data.get("state", "standby").upper()
        color = C["accent"] if state == "PRINTING" else (C["accent2"] if state == "PAUSED" else C["text_dim"])
        self.state_lbl.setText(state)
        self.state_lbl.setStyleSheet(f"color:{color}; font-size:10px; font-weight:bold; background:transparent;")

        ht  = data.get("hotend_temp",  0.0)
        htg = data.get("hotend_target",0.0)
        bt  = data.get("bed_temp",     0.0)
        btg = data.get("bed_target",   0.0)
        self.hotend_lbl.setText(f"🔥 {ht:.0f}° / {htg:.0f}°")
        self.bed_lbl.setText(   f"🛏 {bt:.0f}° / {btg:.0f}°")

        prog = data.get("progress", 0.0)
        self.progress_lbl.setText(f"{prog*100:.1f}%")
        self.progress_mini.setValue(prog)


# ─── Skyblock Sidebar ─────────────────────────────────────────────────────────

class SkyblockSidebar(QWidget):
    def __init__(self, free_will_anchor: float, parent=None):
        super().__init__(parent)
        self._anchor = free_will_anchor
        self.setFixedWidth(200)
        self.setStyleSheet(f"background: {C['bg_card']}; border-left: 1px solid {C['border']};")
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 18, 14, 18)
        layout.setSpacing(20)

        hdr = label("SKYBLOCK", C["accent3"], 10, bold=True)
        hdr.setStyleSheet(hdr.styleSheet() + "letter-spacing: 3px;")
        layout.addWidget(hdr)
        layout.addWidget(SeparatorLine())

        # ── Cult of the Fallen Star ───
        cult_section = QWidget()
        cult_layout  = QVBoxLayout(cult_section)
        cult_layout.setContentsMargins(0,0,0,0)
        cult_layout.setSpacing(4)

        cult_icon  = label("☄", C["accent3"], 22)
        cult_title = label("Cult of the\nFallen Star", C["text_primary"], 11, bold=True)
        cult_title.setWordWrap(True)

        self.cult_sb_time  = label("SB --:--", C["text_secondary"], 9)
        self.cult_countdown= label("--:--:--",   C["accent3"],       20, bold=True)
        self.cult_date_lbl = label("Next: ...",  C["text_dim"],       9)

        cult_layout.addWidget(cult_icon)
        cult_layout.addWidget(cult_title)
        cult_layout.addWidget(self.cult_sb_time)
        cult_layout.addWidget(self.cult_countdown)
        cult_layout.addWidget(self.cult_date_lbl)
        layout.addWidget(cult_section)

        layout.addWidget(SeparatorLine())

        # ── Free Will / Rift ──────────
        fw_section = QWidget()
        fw_layout  = QVBoxLayout(fw_section)
        fw_layout.setContentsMargins(0,0,0,0)
        fw_layout.setSpacing(4)

        fw_icon  = label("⧗", C["accent2"], 22)
        fw_title = label("Free Will\n/ Rift",    C["text_primary"], 11, bold=True)
        fw_title.setWordWrap(True)

        self.fw_countdown   = label("--:--:--",  C["accent2"], 20, bold=True)
        self.fw_progress    = AnimatedProgressBar()
        self.fw_cycle_label = label(f"Cycle: {FREE_WILL_CYCLE_HRS}h", C["text_dim"], 9)

        fw_layout.addWidget(fw_icon)
        fw_layout.addWidget(fw_title)
        fw_layout.addWidget(self.fw_countdown)
        fw_layout.addWidget(self.fw_progress)
        fw_layout.addWidget(self.fw_cycle_label)
        layout.addWidget(fw_section)

        layout.addStretch()

        hint = label("[S] toggle sidebar", C["text_dim"], 8)
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)

    def update_timers(self):
        now = time.time()
        sb  = SkyblockTimers.real_to_sb(now)

        # Cult
        cult_remaining = SkyblockTimers.next_cult_event(now)
        self.cult_countdown.setText(fmt_countdown(cult_remaining))
        self.cult_sb_time.setText(f"SB {sb['hour']:02d}:{sb['min']:02d}  Day {sb['day']}")
        # Approximate next cult day
        cult_h = int(cult_remaining // 3600)
        cult_m = int((cult_remaining % 3600) // 60)
        self.cult_date_lbl.setText(f"In {cult_h}h {cult_m}m real time")

        # Free Will
        fw_remaining = SkyblockTimers.free_will_remaining(now, self._anchor)
        fw_total     = FREE_WILL_CYCLE_HRS * 3600
        self.fw_countdown.setText(fmt_countdown(fw_remaining))
        self.fw_progress.setValue(fw_remaining / fw_total)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    VIEW_NOW_PLAYING = 0
    VIEW_PLAYLISTS   = 1

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Custom Player")
        self.setMinimumSize(800, 480)
        self._current_view   = self.VIEW_NOW_PLAYING
        self._sidebar_visible= True
        self._current_track  = MOCK_TRACK.copy()

        # Free Will anchor = now (first launch; persist across runs via a file if needed)
        self._fw_anchor = time.time()
        self._load_fw_anchor()

        self._build_ui()
        self._apply_stylesheet()
        self._wire_workers()
        self._wire_input()
        self._start_timers()

    # ── Free-will anchor persistence ─────────────────────────────────────────
    def _anchor_path(self):
        return os.path.join(os.path.dirname(__file__), ".fw_anchor")

    def _load_fw_anchor(self):
        try:
            with open(self._anchor_path()) as f:
                self._fw_anchor = float(f.read().strip())
        except:
            self._save_fw_anchor()

    def _save_fw_anchor(self):
        try:
            with open(self._anchor_path(), "w") as f:
                f.write(str(self._fw_anchor))
        except: pass

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top: content row ─────────────────────────────────────────────────
        content_row = QHBoxLayout()
        content_row.setSpacing(0)

        # Main stacked area
        self.main_stack = QStackedWidget()
        self.now_playing = NowPlayingView()
        self.playlist_view = PlaylistView()
        self.main_stack.addWidget(self.now_playing)
        self.main_stack.addWidget(self.playlist_view)
        self.main_stack.setCurrentIndex(self.VIEW_NOW_PLAYING)
        content_row.addWidget(self.main_stack, 1)

        # Sidebar
        self.sidebar = SkyblockSidebar(self._fw_anchor)
        content_row.addWidget(self.sidebar)

        root.addLayout(content_row, 1)

        # ── Bottom: printer footer ────────────────────────────────────────────
        self.footer = PrinterFooter()
        root.addWidget(self.footer)

    def _apply_stylesheet(self):
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {C['bg']};
                color: {C['text_primary']};
                font-family: 'Noto Sans', 'DejaVu Sans', sans-serif;
            }}
            QScrollBar:vertical {{
                background: {C['bg_card']}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C['border']}; border-radius: 3px;
            }}
        """)

    # ── Workers ───────────────────────────────────────────────────────────────
    def _wire_workers(self):
        self.mopidy_worker = MopidyWorker()
        self.mopidy_worker.track_updated.connect(self._on_track_update)
        self.mopidy_worker.playlists_updated.connect(self.playlist_view.update_playlists)
        self.mopidy_worker.start()

        self.printer_worker = MoonrakerWorker()
        self.printer_worker.printer_updated.connect(self.footer.update_printer)
        self.printer_worker.start()

        # Wire now-playing controls → worker
        np = self.now_playing
        np.play_pause_requested.connect(self.mopidy_worker.play_pause)
        np.next_requested.connect(self.mopidy_worker.next_track)
        np.prev_requested.connect(self.mopidy_worker.prev_track)
        np.shuffle_requested.connect(lambda: self.mopidy_worker.shuffle(not self._current_track.get("shuffle")))
        np.loop_requested.connect(lambda: self.mopidy_worker.loop(not self._current_track.get("repeat")))
        self.playlist_view.playlist_selected.connect(self.mopidy_worker.play_playlist)

    # ── Input handler ─────────────────────────────────────────────────────────
    def _wire_input(self):
        self.input_handler = InputHandler()
        ih = self.input_handler

        ih.encoder_up.connect(self._on_encoder_up)
        ih.encoder_down.connect(self._on_encoder_down)
        ih.encoder_click.connect(self._on_encoder_click)

        ih.btn_play_pause.connect(self.mopidy_worker.play_pause)
        ih.btn_next.connect(self.mopidy_worker.next_track)
        ih.btn_prev.connect(self.mopidy_worker.prev_track)
        ih.btn_shuffle.connect(lambda: self.mopidy_worker.shuffle(not self._current_track.get("shuffle")))
        ih.btn_loop.connect(lambda: self.mopidy_worker.loop(not self._current_track.get("repeat")))
        ih.btn_sidebar_toggle.connect(self._toggle_sidebar)

    # ── Qt Key forwarding ─────────────────────────────────────────────────────
    def keyPressEvent(self, event):
        if not self.input_handler.handle_key_event(event.key()):
            super().keyPressEvent(event)

    # ── Timers ────────────────────────────────────────────────────────────────
    def _start_timers(self):
        self._sb_timer = QTimer(self)
        self._sb_timer.timeout.connect(self.sidebar.update_timers)
        self._sb_timer.start(POLL_INTERVAL_MS_SKYBLOCK)

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_track_update(self, track: dict):
        self._current_track = track
        self.now_playing.update_track(track)

    def _on_encoder_up(self):
        if self._current_view == self.VIEW_PLAYLISTS:
            self.playlist_view.scroll_up()

    def _on_encoder_down(self):
        if self._current_view == self.VIEW_PLAYLISTS:
            self.playlist_view.scroll_down()

    def _on_encoder_click(self):
        if self._current_view == self.VIEW_NOW_PLAYING:
            self._current_view = self.VIEW_PLAYLISTS
        else:
            # If a playlist is highlighted, play it; then switch back
            item = self.playlist_view.list_widget.currentItem()
            if item:
                self.mopidy_worker.play_playlist(item.text())
            self._current_view = self.VIEW_NOW_PLAYING
        self.main_stack.setCurrentIndex(self._current_view)

    def _toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        self.sidebar.setVisible(self._sidebar_visible)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        self.mopidy_worker.stop()
        self.printer_worker.stop()
        self.mopidy_worker.wait(2000)
        self.printer_worker.wait(2000)
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # On Pi with EGLFS / no X11, set platform early:
    # os.environ.setdefault("QT_QPA_PLATFORM", "eglfs")
    # os.environ.setdefault("QT_QPA_EGLFS_HIDECURSOR", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("CustomPlayer")
    app.setApplicationVersion("1.0.0")

    # Dark palette baseline
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(C["bg"]))
    palette.setColor(QPalette.WindowText,      QColor(C["text_primary"]))
    palette.setColor(QPalette.Base,            QColor(C["bg_card"]))
    palette.setColor(QPalette.AlternateBase,   QColor(C["bg_elevated"]))
    palette.setColor(QPalette.Text,            QColor(C["text_primary"]))
    palette.setColor(QPalette.Button,          QColor(C["bg_elevated"]))
    palette.setColor(QPalette.ButtonText,      QColor(C["text_primary"]))
    palette.setColor(QPalette.Highlight,       QColor(C["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor(C["bg"]))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
