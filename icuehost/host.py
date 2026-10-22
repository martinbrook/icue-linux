"""QtWebEngine host: one QWebEngineView per widget slot, with the iCUE runtime
shim injected and plugins published over QWebChannel."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QLocale, QUrl, Qt
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineScript,
                                     QWebEngineSettings)
from PySide6.QtWebEngineWidgets import QWebEngineView

from . import widget as wmod
from .expressions import ExpressionEngine
from .plugins import PluginHub

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "icue-linux"
SHIM = (Path(__file__).with_name("shim.js")).read_text(encoding="utf-8")
# The Edge's own UUID is not visible on Linux; a fixed synthetic id is enough
# for widgets that only pass it back to plugins.
EDGE_DEVICE_ID = "6c1e0e0e-0000-4000-8000-00000ed90001"

_QWEBCHANNEL_JS: str | None = None


def qwebchannel_js() -> str:
    global _QWEBCHANNEL_JS
    if _QWEBCHANNEL_JS is None:
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if not f.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("qwebchannel.js resource not found (is QtWebChannel installed?)")
        _QWEBCHANNEL_JS = bytes(f.readAll()).decode()
    return _QWEBCHANNEL_JS


def default_language() -> str:
    name = QLocale.system().name()          # en_GB, zh_CN ...
    return name if name in ("zh_CN", "zh_TW") else name.split("_")[0]


class Settings:
    """Per-instance persisted property values (what iCUE's settings panel stores)."""

    def __init__(self, instance: str):
        self.path = CONFIG_DIR / "instances" / f"{instance}.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("values", {})
        if "uniqueId" not in self.data:      # must be stable: widgets key localStorage on it
            self.data["uniqueId"] = str(uuid.uuid4())
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


class WidgetInstance:
    """A loaded widget, its evaluated controls and current property values."""

    def __init__(self, path: str | Path, hub: PluginHub, instance: str | None = None,
                 language: str | None = None, temperature_unit: str = "°C",
                 overrides: dict | None = None):
        self.widget = wmod.load(path)
        self.hub = hub
        self.instance = instance or self.widget.id
        self.settings = Settings(self.instance)
        self.icue = {"iCUELanguage": language or default_language(), "fpsLimit": 30,
                     "isPreview": False, "temperatureUnit": temperature_unit}
        self.plugin_objs = {p.key: hub.get(p.module) for p in self.widget.plugins}
        ex = ExpressionEngine(self.widget, self.icue, self.plugin_objs)
        self.title = ex.eval_title(self.widget.title_expr) or self.widget.manifest.get("name", "")
        self.controls = []
        for c in self.widget.controls:
            ev = {k: ex.eval(v) for k, v in c.attrs.items()}
            ev.setdefault("label", c.name)
            self.controls.append({"name": c.name, "type": c.type, **ev})
        self.groups = []
        for g in self.widget.groups_raw:
            self.groups.append({"title": ex.eval_title(str(g.get("title", ""))),
                                "info": ex.eval_title(str(g.get("info", ""))) if g.get("info") else "",
                                "properties": g.get("properties", [])})
        self.values = {c["name"]: c.get("default") for c in self.controls}
        self.values.update({k: v for k, v in self.settings.data["values"].items() if k in self.values})
        if overrides:
            self.set_values(overrides)

    def set_values(self, values: dict, persist: bool = True):
        self.values.update(values)
        if persist:
            self.settings.data["values"].update(values)
            self.settings.save()

    def shim_source(self) -> str:
        cfg = {
            "properties": self.values,
            "iCUE": {k: v for k, v in self.icue.items() if k != "temperatureUnit"},
            "temperatureUnit": self.icue["temperatureUnit"],
            "deviceId": EDGE_DEVICE_ID,
            "uniqueId": self.settings.data["uniqueId"],
            "lang": self.icue["iCUELanguage"],
            "translations": self.widget.translations,
            "plugins": [{"key": k, "describe": o.describe(), "ctx": o.blocking_context()}
                        for k, o in self.plugin_objs.items()],
        }
        js = SHIM.replace("__ICUE_CFG__", json.dumps(cfg, ensure_ascii=False))
        return qwebchannel_js() + "\n" + js


_PROFILE: QWebEngineProfile | None = None
_VIEWS: list = []


def shutdown() -> None:
    """Tear down in the order QtWebEngine needs (pages, then views, then the
    profile). Leaving it to Python's exit order can abort with
    'free(): invalid pointer'. Deleting the profile also flushes localStorage."""
    global _PROFILE
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    for v in _VIEWS:
        try:
            v.hide()
            v.page().deleteLater()
        except RuntimeError:            # already gone
            pass
    app.processEvents()
    for v in _VIEWS:
        try:
            v.deleteLater()
        except RuntimeError:
            pass
    _VIEWS.clear()
    app.processEvents()
    if _PROFILE is not None:
        _PROFILE.deleteLater()
        _PROFILE = None
        app.processEvents()


def profile() -> QWebEngineProfile:
    """Persistent profile so widget localStorage survives restarts."""
    global _PROFILE
    if _PROFILE is None:
        from PySide6.QtWidgets import QApplication
        _PROFILE = QWebEngineProfile("icue-linux", QApplication.instance())
        _PROFILE.setPersistentStoragePath(str(CONFIG_DIR / "webengine"))
        _PROFILE.setHttpUserAgent(_PROFILE.httpUserAgent().replace("Linux", "Windows NT 10.0") +
                                  " iCUE-Linux/0.1")
    return _PROFILE


class LoggingPage(QWebEnginePage):
    def __init__(self, prof, tag, parent=None):
        super().__init__(prof, parent)
        self.tag = tag
        self.console: list[str] = []

    def javaScriptConsoleMessage(self, level, message, line, source):
        entry = f"[{self.tag}] {message} ({Path(source).name}:{line})"
        self.console.append(entry)
        if os.environ.get("ICUE_LINUX_QUIET") != "1":
            print(entry)


class WidgetView(QWebEngineView):
    def __init__(self, inst: WidgetInstance, parent=None):
        super().__init__(parent)
        self.inst = inst
        page = LoggingPage(profile(), inst.instance, self)
        _VIEWS.append(self)
        self.setPage(page)
        s = page.settings()
        # Widgets are file:// pages that need both their own files (CSS, scripts)
        # and the web (album art, weather APIs). In QtWebEngine, enabling remote
        # access on its own stops local stylesheets applying; setting both makes
        # both work. fetch() to web APIs then follows normal CORS rules.
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        page.setBackgroundColor(QColor(Qt.GlobalColor.transparent))

        self.channel = QWebChannel(page)
        for key, obj in inst.plugin_objs.items():
            self.channel.registerObject(key, obj)
        page.setWebChannel(self.channel)

        script = QWebEngineScript()
        script.setName("icue-shim")
        script.setSourceCode(inst.shim_source())
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        page.scripts().insert(script)

        if not inst.widget.interactive:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.load(QUrl.fromLocalFile(str(inst.widget.index.resolve())))

    def push_values(self, values: dict):
        self.inst.set_values(values)
        self.page().runJavaScript(f"window.__icueHost && __icueHost.setProperties({json.dumps(values)})")
