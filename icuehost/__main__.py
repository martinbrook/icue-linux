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
    from PySide6.QtCore import qInstallMessageHandler

    def _qt_log(mode, ctx, msg):
        # Expected: our Edge windows refuse focus on purpose (see _no_focus).
        if "WindowDoesNotAcceptFocus" in msg:
            return
        sys.stderr.write(msg + "\n")
    qInstallMessageHandler(_qt_log)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("icue-linux")
    app.setDesktopFileName("icue-linux")      # Wayland app_id: lets the GNOME extension recognise us
    return app


def _no_focus(w):
    """Touching the Edge must not take keyboard focus from the app you're working
    in, or Deck keystrokes would land on the Edge itself. Honoured on X11 and by
    most Wayland compositors for this window type."""
    from PySide6.QtCore import Qt
    w.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)


def _exit(app, rc=None):
    """Run the event loop (unless rc given), tear WebEngine down cleanly, exit
    without letting Python's finaliser touch Qt objects again."""
    import os
    import signal
    from PySide6.QtCore import QTimer
    from .host import shutdown
    if rc is None:
        # Ctrl+C / kill: Qt's loop never hands control back to Python, so wake it
        # regularly to let the signal handlers run, then quit cleanly.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: app.quit())
        app._sigtimer = QTimer()
        app._sigtimer.timeout.connect(lambda: None)
        app._sigtimer.start(200)
        rc = app.exec()
    shutdown()
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(rc)


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
    if not a.focus:
        _no_focus(v)
    from PySide6.QtGui import QColor
    v.page().setBackgroundColor(QColor(a.background))   # the Edge's dark canvas
    v.setWindowTitle(inst.title)
    v.resize(*_size(a.size))
    v.show()
    if a.settings:
        from .settings_ui import SettingsPanel
        v._panel = SettingsPanel(v)
        v._panel.show()
    if a.quit_after:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(a.quit_after * 1000, app.quit)
    _exit(app)


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

    target = Path(a.layout).expanduser()
    if target.is_dir() or target.suffix in (".icuewidget", ".zip"):
        # a single widget: fill the Edge with it
        cfg = {"widget": [{"path": str(target.resolve()), "x": 0, "y": 0, "size": "full"}]}
    else:
        cfg = tomllib.loads(target.read_text())
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
    if not disp.get("take_focus", False):
        _no_focus(win)
    win.setStyleSheet(f"background:{disp.get('background', '#000')};")
    win.setGeometry(screen.geometry())
    win.views = []
    for spec in cfg.get("widget", []):
        wpath = Path(spec["path"]).expanduser()
        if not wpath.is_absolute() and not wpath.exists():      # relative to the layout file
            wpath = Path(a.layout).expanduser().resolve().parent / wpath
        if not wpath.exists():
            print(f"[icue-linux] skipping {spec.get('instance') or spec['path']}: {wpath} not found")
            continue
        inst = WidgetInstance(wpath, hub, instance=spec.get("instance"),
                              language=disp.get("language"),
                              temperature_unit=disp.get("temperature_unit", "°C"),
                              overrides=spec.get("values"))
        v = WidgetView(inst, win)
        w, h = _size(spec.get("size", "small"))
        v.setGeometry(spec.get("x", 0), spec.get("y", 0), spec.get("w", w), spec.get("h", h))
        win.views.append(v)
    # Pick the screen before the window is mapped: Wayland compositors ignore
    # moves after the fact, so setting it later can leave us on the wrong monitor.
    win.winId()
    win.windowHandle().setScreen(screen)
    win.setGeometry(screen.geometry())
    win.showFullScreen()
    if a.quit_after:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(a.quit_after * 1000, app.quit)
    _exit(app)


SERVICE = "icue-linux.service"


def cmd_service(a):
    """Run a layout as a systemd user service: starts with your desktop session,
    restarts within seconds if it ever crashes."""
    import subprocess
    unit = Path.home() / ".config/systemd/user" / SERVICE
    sc = lambda *args: subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)  # noqa: E731
    if a.action == "remove":
        sc("disable", "--now", SERVICE)
        unit.unlink(missing_ok=True)
        sc("daemon-reload")
        print("Removed the icue-linux service.")
        return
    if a.action == "status":
        print(sc("status", "--no-pager", SERVICE).stdout or "not installed")
        return
    layout = Path(a.layout).expanduser().resolve()
    if not layout.exists():
        sys.exit(f"{layout} not found")
    workdir = Path.cwd().resolve()     # so relative widget paths in the layout keep working
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f"""[Unit]
Description=icue-linux widgets on the Xeneon Edge
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={sys.executable} -m icuehost run {layout}
WorkingDirectory={workdir}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
""")
    sc("daemon-reload")
    r = sc("enable", "--now", SERVICE)
    if r.returncode:
        sys.exit("systemctl failed: " + r.stderr.strip())
    print(f"Installed and started {SERVICE} running {layout}")
    print("It starts with your desktop session and restarts itself if it crashes.")
    print("  logs:    journalctl --user -u icue-linux -f")
    print("  restart: systemctl --user restart icue-linux   (after editing the layout)")
    print("  stop:    systemctl --user stop icue-linux")


EXT_UUID = "edge-guard@icue-linux"


def cmd_gnome_setup(a):
    """Install (or remove) the GNOME Shell extension that keeps new app windows
    off the Edge. Wayland doesn't let other programs move windows, so this has
    to live inside GNOME Shell."""
    import re
    import shutil
    import subprocess
    dest = Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID
    if a.remove:
        subprocess.run(["gnome-extensions", "disable", EXT_UUID], capture_output=True)
        shutil.rmtree(dest, ignore_errors=True)
        print(f"Removed {EXT_UUID}. Log out and back in to unload it fully.")
        return
    try:
        out = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout
        major = int(re.search(r"(\d+)", out).group(1))
    except (OSError, AttributeError, ValueError):
        sys.exit("gnome-shell not found: this is only needed on GNOME.")
    src = Path(__file__).with_name("gnome")
    common = (src / "common.js.in").read_text()
    variant = "extension.esm.js.in" if major >= 45 else "extension.legacy.js.in"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "extension.js").write_text((src / variant).read_text().replace("/*COMMON*/", common))
    (dest / "metadata.json").write_text(json.dumps({
        "uuid": EXT_UUID, "name": "icue-linux Edge guard",
        "description": "Opens new app windows on the main monitor instead of the Xeneon Edge.",
        "shell-version": [str(major)], "version": 1}, indent=2))
    subprocess.run(["gsettings", "set", "org.gnome.shell", "disable-user-extensions", "false"],
                   capture_output=True)
    r = subprocess.run(["gnome-extensions", "enable", EXT_UUID], capture_output=True, text=True)
    print(f"Installed {EXT_UUID} for GNOME {major} in {dest}")
    if r.returncode == 0:
        print("Enabled. On Wayland, log out and back in once so GNOME Shell loads it.")
    else:
        print("Log out and back in, then run:\n  gnome-extensions enable " + EXT_UUID)


def find_touchscreens(text: str) -> list[dict]:
    """Parse /proc/bus/input/devices for direct-touch devices (touchscreens).
    INPUT_PROP_DIRECT (bit 1 of PROP) marks screens rather than touchpads."""
    out = []
    for block in text.strip().split("\n\n"):
        info = {}
        for line in block.splitlines():
            if line.startswith("I:"):
                info.update(dict(kv.split("=", 1) for kv in line[3:].split()))
            elif line.startswith("N: Name="):
                info["name"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("B: PROP="):
                info["prop"] = int(line.split("=", 1)[1].split()[-1], 16)
            elif line.startswith("B: ABS="):
                info["abs"] = line.split("=", 1)[1]
            elif line.startswith("H: Handlers="):
                info["handlers"] = line.split("=", 1)[1]
        if info.get("prop", 0) & 0x2 and info.get("abs") and "event" in info.get("handlers", ""):
            name = info.get("name", "")
            if "pen" in name.lower() and "touch" not in name.lower():
                continue                                   # tablets / styluses
            out.append({"name": name, "vendor": info.get("Vendor", "0000").lower(),
                        "product": info.get("Product", "0000").lower()})
    return out


def find_monitor(gdbus_text: str, match: str):
    """(connector, vendor, product, serial) tuples from Mutter's GetCurrentState."""
    import re
    for conn, vendor, product, serial in re.findall(
            r"\('([A-Za-z0-9-]+)', '([^']*)', '([^']*)', '([^']*)'\)", gdbus_text):
        if match.lower() in (conn + " " + vendor + " " + product).lower():
            return conn, vendor, product, serial
    return None


def cmd_touch_setup(a):
    """Map the Edge's touch panel to the Edge's display (GNOME on Wayland)."""
    import subprocess
    devs = find_touchscreens(Path("/proc/bus/input/devices").read_text())
    if not devs:
        sys.exit("No touchscreen found. Is the Edge's USB-C cable plugged into this PC? "
                 "(HDMI/DisplayPort carry the picture only; touch comes over USB.)")
    if a.device:
        pick = [d for d in devs if f"{d['vendor']}:{d['product']}" == a.device.lower()]
        if not pick:
            sys.exit(f"No touchscreen {a.device}. Found: " +
                     ", ".join(f"{d['vendor']}:{d['product']} ({d['name']})" for d in devs))
    else:
        pick = [d for d in devs if any(k in d["name"].lower() for k in ("xeneon", "corsair", "edge"))]
        if not pick and len(devs) == 1:
            pick = devs
    if not pick:
        # e.g. a laptop with its own touchscreen plus the Edge under a generic name
        print("More than one touchscreen and none is obviously the Edge:")
        for d in devs:
            print(f"  {d['vendor']}:{d['product']}  {d['name']}")
        sys.exit("Unplug the Edge's USB-C to see which one disappears, then run:\n"
                 "  icue-linux touch-setup --device VVVV:PPPP")
    dev = pick[0]
    try:
        state = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.DisplayConfig",
             "--object-path", "/org/gnome/Mutter/DisplayConfig",
             "--method", "org.gnome.Mutter.DisplayConfig.GetCurrentState"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        state = ""
    mon = find_monitor(state, a.match)
    if not mon:
        sys.exit("Couldn't find the Edge in GNOME's display list (is this a GNOME session?). "
                 "On X11 use: xinput map-to-output '<touch device name>' DP-2")
    conn, vendor, product, serial = mon
    path = f"/org/gnome/desktop/peripherals/touchscreens/{dev['vendor']}:{dev['product']}/"
    value = f"['{vendor}', '{product}', '{serial}']"
    cmd = ["gsettings", "set", f"org.gnome.desktop.peripherals.touchscreen:{path}", "output", value]
    print(f"Touchscreen: {dev['name']} ({dev['vendor']}:{dev['product']})")
    print(f"Display:     {conn}  {vendor} {product} {serial}")
    if a.dry_run:
        print("Would run:\n  " + " ".join(cmd[:3]) + f' output "{value}"')
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("gsettings failed: " + r.stderr.strip())
    print("Done. Touches on the Edge now map to the Edge (takes effect immediately;\n"
          "if not, unplug and replug the Edge's USB-C cable).")


def _crash_log():
    """On a native crash (SIGSEGV/SIGABRT...) write every thread's Python stack
    to ~/.cache/icue-linux/crash.log, so the next 'free(): invalid pointer'
    says where it happened."""
    import faulthandler
    import os
    import time
    d = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "icue-linux"
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = open(d / "crash.log", "a", buffering=1)
        f.write(f"\n=== icue-linux started {time.strftime('%Y-%m-%d %H:%M:%S')} pid {os.getpid()} "
                f"argv {sys.argv[1:]}\n")
        faulthandler.enable(file=f, all_threads=True)
        sys._icue_crash_log = f            # keep the file open for the process lifetime
    except OSError:
        faulthandler.enable()


def main(argv=None):
    _crash_log()
    ap = argparse.ArgumentParser(prog="icue-linux")
    ap.add_argument("--lang", default=None, help="iCUE language code (default: system)")
    ap.add_argument("--temp-unit", default="°C", choices=["°C", "°F"])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview"); p.add_argument("widget")
    p.add_argument("--size", default="small", help="small|medium|large|xl|full or WxH")
    p.add_argument("--instance"); p.add_argument("--set", action="append")
    p.add_argument("--settings", action="store_true", help="open the generated settings panel")
    p.add_argument("--background", default="#000000")
    p.add_argument("--focus", action="store_true", help="let the preview window take keyboard focus")
    p.add_argument("--quit-after", type=int, default=0, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_preview)
    p = sub.add_parser("info"); p.add_argument("widget"); p.add_argument("--instance")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("run"); p.add_argument("layout", help="layout .toml, or a single widget to show full-screen")
    p.add_argument("--quit-after", type=int, default=0, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("screens"); p.set_defaults(fn=cmd_screens)
    p = sub.add_parser("service", help="run a layout at login and restart it if it crashes")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.add_argument("layout", nargs="?", default="examples/edge-layout.toml")
    p.set_defaults(fn=cmd_service)
    p = sub.add_parser("touch-setup", help="map the Edge's touch panel to the Edge display (GNOME)")
    p.add_argument("--match", default="XENEON", help="text identifying the Edge display")
    p.add_argument("--device", help="touchscreen as vendor:product, if more than one is found")
    p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=cmd_touch_setup)
    p = sub.add_parser("gnome-setup", help="install the GNOME extension that keeps app windows off the Edge")
    p.add_argument("--remove", action="store_true"); p.set_defaults(fn=cmd_gnome_setup)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
