"""End-to-end check: load the sample widget headless and verify the iCUE
runtime contract from inside the page.

Run:  QT_QPA_PLATFORM=offscreen python tests/smoke_test.py
"""
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["ICUE_LINUX_SYSFS_ROOT"] = str(HERE / "fakesys")
os.environ["XDG_CONFIG_HOME"] = str(HERE / ".cfg")
os.environ.setdefault("ICUE_LINUX_QUIET", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
shutil.rmtree(HERE / ".cfg", ignore_errors=True)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
from icuehost.host import WidgetInstance, WidgetView  # noqa: E402
from icuehost.plugins import PluginHub  # noqa: E402

temp = HERE / "fakesys/sys/class/hwmon/hwmon0/temp1_input"
temp.write_text("52125\n")

inst = WidgetInstance(ROOT / "examples/sysinfo-widget", PluginHub(), instance="smoke", language="de")
view = WidgetView(inst)
view.resize(840, 344)
view.show()

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def step_change():
    temp.write_text("61500\n")                          # sensor changes -> signal
    view.push_values({"textColor": "#ff0000", "layout": "text"})  # settings change


def collect():
    view.page().runJavaScript("JSON.stringify({t: window.__test, color: document.body.style.color,"
                              " cls: document.body.className, flex: getComputedStyle(document.body).display, rows: document.querySelectorAll('.row').length})",
                              0, report)


def report(raw):
    r = json.loads(raw or "{}")
    t = r.get("t") or {}
    ev = t.get("events", [])
    check("onICUEInitialized" in ev, "onICUEInitialized fired")
    check("sensors:onInitialized" in ev, "Sensors plugin onInitialized fired")
    check("media:onInitialized" in ev, "Media plugin onInitialized fired")
    check(ev.index("sensors:onInitialized") < ev.index("onICUEInitialized") if
          "onICUEInitialized" in ev and "sensors:onInitialized" in ev else False,
          "plugins initialise before iCUE")
    check(ev.count("onDataUpdated") >= 2, "onDataUpdated fired initially and on settings change")
    check(r.get("rows", 0) >= 3, f"sensor rows rendered via async API ({r.get('rows')})")
    tid = "hwmon:k10temp:hwmon0:temp1"
    check(t.get("values", {}).get(tid) == "61.5", f"sensorValueChanged delivered new value ({t.get('values', {}).get(tid)})")
    check(r.get("color") == "rgb(255, 0, 0)", "property update reached the page")
    check(r.get("cls") == "text", "combobox key value applied")
    check(r.get("flex") == "flex", "external stylesheet applied (file:// CSS)")
    check(t.get("tr") == "Textfarbe", "runtime tr() promise translates (de)")
    check((t.get("storage") or {}).get("launches") == 1, "localStorage keyed by uniqueId")
    icue = t.get("icue") or {}
    check(icue.get("lang") == "de" and icue.get("unit") == "°C" and icue.get("preview") is False,
          "iCUE global object")
    check(bool(icue.get("device")), "device.deviceId present")
    view.grab().save(str(HERE / "smoke.png"))
    errors = [c for c in view.page().console if "rror" in c]
    check(not errors, "no JS errors" + (": " + "; ".join(errors) if errors else ""))
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    app.exit(1 if failures else 0)


QTimer.singleShot(3000, step_change)
QTimer.singleShot(5500, collect)
QTimer.singleShot(20000, lambda: (print("timeout"), app.exit(2)))
sys.exit(app.exec())
