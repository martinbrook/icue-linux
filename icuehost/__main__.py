"""icue-linux — run Corsair iCUE HTML widgets on Linux.

  python -m icuehost preview WIDGET [--size 840x344] [--set k=v ...] [--settings]
  python -m icuehost run LAYOUT.toml
  python -m icuehost info WIDGET
  python -m icuehost screens
"""
from __future__ import annotations

import argparse
import json
import sys
try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:             # 3.10: pip install tomli
    import tomli as tomllib
from pathlib import Path

# The Edge slot sizes iCUE uses (horizontal); vertical swaps them.
SLOTS = {"small": (840, 344), "medium": (1688, 344), "large": (840, 696), "xl": (2536, 696),
         "full": (2560, 720)}


def _size(s: str) -> tuple[int, int]:
    if s in SLOTS:
        return SLOTS[s]
    w, h = s.lower().split("x")
    return int(w), int(h)


def _kv(pairs):
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _app():
    from PySide6.QtCore import Qt, QCoreApplication
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("icue-linux")
    return app


def cmd_info(a):
    app = _app()  # noqa: F841  (QJSEngine/QObjects need an app)
    from .host import WidgetInstance
    from .plugins import PluginHub
    inst = WidgetInstance(a.widget, PluginHub(a.temp_unit), instance=a.instance, language=a.lang)
    print(json.dumps({"id": inst.widget.id, "title": inst.title,
                      "plugins": [p.key for p in inst.widget.plugins],
                      "interactive": inst.widget.interactive,
                      "controls": inst.controls, "groups": inst.groups,
                      "values": inst.values}, indent=2, ensure_ascii=False, default=str))


def cmd_preview(a):
    app = _app()
    from .host import WidgetInstance, WidgetView
    from .plugins import PluginHub
    inst = WidgetInstance(a.widget, PluginHub(a.temp_unit), instance=a.instance,
                          language=a.lang, overrides=_kv(a.set))
    v = WidgetView(inst)
    from PySide6.QtGui import QColor
    v.page().setBackgroundColor(QColor(a.background))   # the Edge's dark canvas
    v.setWindowTitle(inst.title)
    v.resize(*_size(a.size))
    v.show()
    if a.settings:
        from .settings_ui import SettingsPanel
        v._panel = SettingsPanel(v)
        v._panel.show()
    sys.exit(app.exec())


def cmd_screens(a):
    app = _app()
    for s in app.screens():
        g = s.geometry()
        print(f"{s.name():12} {s.manufacturer()} {s.model():20} {g.width()}x{g.height()}+{g.x()}+{g.y()}")


def cmd_run(a):
    app = _app()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from .host import WidgetInstance, WidgetView
    from .plugins import PluginHub

    cfg = tomllib.loads(Path(a.layout).expanduser().read_text())
    disp = cfg.get("display", {})
    hub = PluginHub(disp.get("temperature_unit", "°C"))
    match = str(disp.get("match", "XENEON")).lower()
    if match == "primary":
        screen = app.primaryScreen()
    else:
        screen = next((s for s in app.screens()
                       if match in (s.name() + s.model() + s.manufacturer()).lower()), None)
    if screen is None:  # fall back to the first 2560x720 / 720x2560 screen
        screen = next((s for s in app.screens()
                       if sorted((s.size().width(), s.size().height())) == [720, 2560]), None)
    if screen is None:
        sys.exit("No Xeneon Edge found; check `python -m icuehost screens` and set display.match")

    win = QWidget()
    win.setWindowTitle("icue-linux")
    win.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint)
    win.setStyleSheet(f"background:{disp.get('background', '#000')};")
    win.setGeometry(screen.geometry())
    win.views = []
    for spec in cfg.get("widget", []):
        inst = WidgetInstance(spec["path"], hub, instance=spec.get("instance"),
                              language=disp.get("language"),
                              temperature_unit=disp.get("temperature_unit", "°C"),
                              overrides=spec.get("values"))
        v = WidgetView(inst, win)
        w, h = _size(spec.get("size", "small"))
        v.setGeometry(spec.get("x", 0), spec.get("y", 0), spec.get("w", w), spec.get("h", h))
        win.views.append(v)
    win.show()
    win.windowHandle().setScreen(screen)
    win.showFullScreen()
    if a.quit_after:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(a.quit_after * 1000, app.quit)
    sys.exit(app.exec())


def main(argv=None):
    ap = argparse.ArgumentParser(prog="icue-linux")
    ap.add_argument("--lang", default=None, help="iCUE language code (default: system)")
    ap.add_argument("--temp-unit", default="°C", choices=["°C", "°F"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview"); p.add_argument("widget")
    p.add_argument("--size", default="small", help="small|medium|large|xl|full or WxH")
    p.add_argument("--instance"); p.add_argument("--set", action="append")
    p.add_argument("--settings", action="store_true", help="open the generated settings panel")
    p.add_argument("--background", default="#000000")
    p.set_defaults(fn=cmd_preview)
    p = sub.add_parser("info"); p.add_argument("widget"); p.add_argument("--instance")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("run"); p.add_argument("layout")
    p.add_argument("--quit-after", type=int, default=0, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("screens"); p.set_defaults(fn=cmd_screens)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
