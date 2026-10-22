"""Media (MPRIS via playerctl), Link (xdg-open), FPS (MangoHud log, optional)
and stubs for plugins with no Linux equivalent yet."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from .base import PluginBase


def _playerctl(*args: str) -> str:
    if not shutil.which("playerctl"):
        return ""
    try:
        return subprocess.run(["playerctl", *args], capture_output=True, text=True,
                              timeout=1).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


class MediaPlugin(PluginBase):
    """widgetbuilder.mediadataprovider:Media:1.0"""
    MODULE = "widgetbuilder.mediadataprovider"

    songNameChanged = Signal()
    artistChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._song, self._artist = "", ""
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.poll)
        self.poll()

    def start(self):
        self._timer.start()

    def poll(self):
        out = _playerctl("metadata", "--format", "{{title}}\t{{artist}}")
        song, _, artist = out.partition("\t")
        if song != self._song:
            self._song = song
            self.songNameChanged.emit()
        if artist != self._artist:
            self._artist = artist
            self.artistChanged.emit()

    def _get_song(self): return self._song
    def _get_artist(self): return self._artist
    songName = Property(str, _get_song, notify=songNameChanged)
    artist = Property(str, _get_artist, notify=artistChanged)

    @Slot(int)
    def getSongName(self, rid): self.respond(rid, self._song)

    @Slot(int)
    def getArtist(self, rid): self.respond(rid, self._artist)

    @Slot()
    def triggerPlayPause(self): _playerctl("play-pause"); self.poll()

    @Slot()
    def triggerNextTrack(self): _playerctl("next"); QTimer.singleShot(300, self.poll)

    @Slot()
    def triggerPreviousTrack(self): _playerctl("previous"); QTimer.singleShot(300, self.poll)


class LinkPlugin(PluginBase):
    """widgetbuilder.linkprovider:Url:1.0"""
    MODULE = "widgetbuilder.linkprovider"

    @Slot(str)
    def open(self, link):
        url = QUrl(link)
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)


class FpsPlugin(PluginBase):
    """widgetbuilder.fpsdataprovider:Fps:1.0

    Reads the newest MangoHud CSV log in $ICUE_LINUX_MANGOHUD_LOGS (default
    ~/.local/share/MangoHud) if MangoHud logging is enabled; otherwise
    reports FPS as unavailable."""
    MODULE = "widgetbuilder.fpsdataprovider"

    fpsUpdated = Signal(int)
    fpsAvailabilityChanged = Signal(bool)
    processChanged = Signal(str)
    currentFpsChanged = Signal()
    fpsAvailableChanged = Signal()
    currentProcessChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fps, self._avail, self._proc = 0, False, ""
        self._dir = Path(os.environ.get("ICUE_LINUX_MANGOHUD_LOGS",
                                        Path.home() / ".local/share/MangoHud"))
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.poll)

    def start(self):
        self._timer.start()

    def poll(self):
        fps, proc, avail = 0, "", False
        try:
            logs = sorted(self._dir.glob("*.csv"), key=lambda p: p.stat().st_mtime)
            if logs and (os.path.getmtime(logs[-1]) > __import__("time").time() - 5):
                last = logs[-1].read_text(errors="ignore").strip().splitlines()[-1]
                fps = int(float(last.split(",")[0]))
                proc = logs[-1].name.rsplit("_", 2)[0]
                avail = True
        except (OSError, ValueError, IndexError):
            pass
        if avail != self._avail:
            self._avail = avail
            self.fpsAvailabilityChanged.emit(avail); self.fpsAvailableChanged.emit()
        if fps != self._fps:
            self._fps = fps
            self.fpsUpdated.emit(fps); self.currentFpsChanged.emit()
        if proc != self._proc:
            self._proc = proc
            self.processChanged.emit(proc); self.currentProcessChanged.emit()

    currentFps = Property(int, lambda s: s._fps, notify=currentFpsChanged)
    fpsAvailable = Property(bool, lambda s: s._avail, notify=fpsAvailableChanged)
    currentProcess = Property(str, lambda s: s._proc, notify=currentProcessChanged)

    @Slot(int)
    def getCurrentFps(self, rid): self.respond(rid, self._fps)

    @Slot(int)
    def getFpsAvailable(self, rid): self.respond(rid, self._avail)

    @Slot(int)
    def getCurrentProcess(self, rid): self.respond(rid, self._proc)


class DeviceActionPlugin(PluginBase):
    """widgetbuilder.deviceactionprovider — the Edge has no dial/keys; widgets
    can initialise but will never receive events."""
    MODULE = "widgetbuilder.deviceactionprovider"
    dialTriggered = Signal("QVariant")
    keyTriggered = Signal("QVariant")

    @Slot(str)
    def initDevice(self, device_id):
        pass


class StubPlugin(PluginBase):
    """Any plugin we don't implement: present, initialised, silent."""


REGISTRY = {
    "widgetbuilder.mediadataprovider": MediaPlugin,
    "widgetbuilder.linkprovider": LinkPlugin,
    "widgetbuilder.fpsdataprovider": FpsPlugin,
    "widgetbuilder.deviceactionprovider": DeviceActionPlugin,
}
