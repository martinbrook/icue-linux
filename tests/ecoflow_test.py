"""EcoFlow widget + plugin against a fake Home Assistant.

  QT_QPA_PLATFORM=offscreen python tests/ecoflow_test.py [outdir]
"""
import json, os, shutil, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
CFG = HERE / ".cfg-ecoflow"
shutil.rmtree(CFG, ignore_errors=True); (CFG / "icue-linux").mkdir(parents=True)
os.environ.update({"XDG_CONFIG_HOME": str(CFG), "QT_QPA_PLATFORM": "offscreen", "ICUE_LINUX_QUIET": "1",
                   "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
sys.path.insert(0, str(ROOT))

# rows as HA's template returns them: [device id, name, model, object id, state]
DELTA = [["d1", "Delta", "DELTA_2_MAX", f"delta_{k}", v] for k, v in {
    "battery_level": "47", "main_battery_level": "47", "slave_1_battery_level": "46",
    "slave_2_battery_level": "unknown", "max_charge_level": "80", "min_discharge_level": "10", "total_in_power": "1762",
    "total_out_power": "1117", "charge_remaining_time": "137", "discharge_remaining_time": "0",
    "status": "online", "slave_1_cycles": "12"}.items()]
RIVER = [["d2", "Garage River", "RIVER_2", f"garage_river_{k}", v] for k, v in {
    "battery_level": "12", "total_in_power": "0", "total_out_power": "240",
    "discharge_remaining_time": "75", "status": "online"}.items()]
PLUG = [["d3", "Smart Plug", "SMART_PLUG", "smart_plug_power", "15"], ["d3", "Smart Plug", "SMART_PLUG", "smart_plug_switch", "on"]]
fake = {"rows": DELTA, "code": 200, "body": None}
seen = []


class FakeHA(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.path, self.headers["Authorization"], body["template"]))
        if self.headers["Authorization"] != "Bearer GOOD":
            return self._reply(401, b'{"message":"Unauthorized"}')
        self._reply(fake["code"], fake["body"] or json.dumps(fake["rows"]).encode())

    def _reply(self, code, body):
        self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHA)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
HA = CFG / "icue-linux/homeassistant.json"
def configure(url=URL, token="GOOD"):
    HA.write_text(json.dumps({"url": url, "token": token}))

from icuehost.plugins import ecoflow as eco
fails = 0
def check(c, m):
    global fails; print(("PASS " if c else "FAIL ") + m); fails += (not c)

# ---- parsing
devs = eco.parse(DELTA + RIVER + PLUG)
check([d["name"] for d in devs] == ["Delta", "Garage River"], f"batteries found, smart plug ignored {[d['name'] for d in devs]}")
d, r = devs
check([(p["label"], p["level"]) for p in d["packs"]] == [("Main", 47.0), ("Extra 1", 46.0), ("Extra 2", None)],
      f"main + extra batteries, unknown kept as null {d['packs']}")
check(d["level"] == 47 and d["limit"] == 80 and d["minLevel"] == 10 and d["inW"] == 1762 and d["chargeMin"] == 137 and d["online"] is True
      and d["model"] == "DELTA 2 MAX", "combined level, charge/discharge limits, power, time, online, model")
check(r["packs"] == [] and r["level"] == 12 and r["limit"] is None, "no extra batteries: no pack rows (two-word device prefix)")

# ---- fetching
HA.unlink(missing_ok=True)
res = eco.fetch(); check(not res["ok"] and "homeassistant.json" in res["error"], f"not set up: {res['error']}")
configure(); res = eco.fetch()
check(res["ok"] and len(res["devices"]) == 1 and seen[-1][0] == "/api/template"
      and "integration_entities('ecoflow_cloud')" in seen[-1][2], "fetch: one template call to HA")
configure(token="BAD"); res = eco.fetch(); check("rejected the token" in res["error"], f"bad token: {res['error']}")
configure(url="http://127.0.0.1:9"); res = eco.fetch(); check("unreachable" in res["error"], f"offline: {res['error']}")
configure(); fake["body"] = b"<html>proxy error</html>"; res = eco.fetch()
check("unexpected reply" in res["error"], f"garbage reply: {res['error']}"); fake["body"] = None
fake["rows"] = PLUG; res = eco.fetch(); check("no EcoFlow batteries" in res["error"], f"no batteries: {res['error']}")
fake["rows"] = DELTA

# ---- plugin polling and the widget
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from icuehost.host import WidgetInstance, WidgetView
from icuehost.plugins import PluginHub

def wait(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

hub = PluginHub(); plugin = hub.get("icuelinux.ecoflow")
got = []; plugin.statusUpdated.connect(lambda j: got.append(json.loads(j)))
seen.clear(); plugin.watch(1000); wait(1500)
check(got and got[-1]["ok"] and plugin.timer.interval() == 5000, "plugin polls off-thread and emits; interval at least 5 s")
n = len(got); plugin.poll(); wait(800)
check(len(got) == n, "unchanged status isn't re-sent to widgets")
plugin.watch(1000); wait(300)
check(len(got) == n + 1, "a widget that (re)connects gets the current status straight away")
plugin.timer.stop()

views = {}
for name, (w, h) in (("small", (840, 344)), ("large", (840, 696))):
    v = WidgetView(WidgetInstance(ROOT / "examples/ecoflow", hub, instance=f"eco-{name}", language="en"))
    v.page().setBackgroundColor(QColor("#000")); v.resize(w, h); v.show(); views[name] = v
wait(4000)

JS = """JSON.stringify({packs: [...document.querySelectorAll('.pack')].map(p => [p.querySelector('.pl').textContent, p.querySelector('.pv').textContent]),
  total: (document.querySelector('.total') || {}).textContent, state: (document.querySelector('.state') || {}).textContent,
  st: ((document.querySelector('.st') || {}).textContent || '').replace(/\\s+/g, ' ').trim(),
  marks: [...(document.querySelector('.bar') || document.body).querySelectorAll('b')].map(b => b.style.left),
  flashing: [...document.querySelectorAll('.pack')].filter(p => getComputedStyle(p.querySelector('.bar i')).animationName === 'flash').map(p => p.querySelector('.pl').textContent),
  red: [...document.querySelectorAll('.pack.low .pv')].map(e => getComputedStyle(e).color), notice: document.getElementById('notice').classList.contains('hidden') ? '' : document.getElementById('notice').textContent,
  overflow: document.documentElement.scrollHeight > innerHeight + 1})"""
def dom(v):
    out = []; loop = QEventLoop(); QTimer.singleShot(3000, loop.quit)        # never hang on a JS error
    v.page().runJavaScript(JS, 0, lambda r: (out.append(json.loads(r) if r else {}), loop.quit())); loop.exec()
    return {"packs": [], "total": None, "state": None, "st": "", "marks": [], "flashing": [], "red": [], "notice": "", "overflow": False,
            **(out[0] if out else {})}

for name, v in views.items():
    r = dom(v); v.grab().save(str(OUT / f"ecoflow-{name}.png"))
    check(r["packs"] == [["Main", "47%"], ["Extra 1", "46%"], ["Extra 2", "–"]] and r["total"] == "47%",
          f"{name}: three packs, unknown pack shows – {r['packs']}")
    check(r["state"] == "charging" and "1.76 kW" in r["st"] and "full in 2 h 17 m" in r["st"],
          f"{name}: charging, power, time to full ({r['st']})")
    check(r["marks"] == ["10%", "80%"], f"{name}: discharge (10%) and charge (80%) limit lines {r['marks']}")
    check(not r["flashing"], f"{name}: nothing flashes at 46-47%")
    check(not r["notice"] and not r["overflow"], f"{name}: no notice, fits the slot")

configure(token="BAD"); plugin.poll(); wait(1500)
r = dom(views["small"]); views["small"].grab().save(str(OUT / "ecoflow-error.png"))
check(r["notice"] == "Home Assistant rejected the token" and not r["packs"], f"error shown in the widget: {r['notice']}")
configure(); fake["rows"] = DELTA + RIVER; plugin.poll(); wait(1500)
r = dom(views["large"]); views["large"].grab().save(str(OUT / "ecoflow-two.png"))
check(len(r["packs"]) == 4 and not r["overflow"], f"two devices fit the large slot ({len(r['packs'])} bars)")

# ---- flashing at 15% and below, per bar
fake["rows"] = [[*row[:4], {"delta_main_battery_level": "16", "delta_slave_1_battery_level": "15",
                            "delta_slave_2_battery_level": "3"}.get(row[3], row[4])] for row in DELTA]
plugin.poll(); wait(1500)
r = dom(views["small"]); views["small"].grab().save(str(OUT / "ecoflow-low.png"))
check(r["flashing"] == ["Extra 1", "Extra 2"], f"15% and 3% flash, 16% doesn't: {r['flashing']}")
check(r["red"] and all(c == "rgb(248, 113, 113)" for c in r["red"]), f"flashing packs' values are red {r['red']}")
fake["rows"] = DELTA + RIVER; plugin.poll(); wait(1500)
r = dom(views["large"])
check("Battery" in [p[0] for p in r["packs"]] and r["flashing"] == ["Battery"], f"single-battery device at 12% flashes {r['flashing']}")

errs = [c for v in views.values() for c in v.page().console if "rror" in c]
check(not errs, "no JS errors" + (": " + "; ".join(errs) if errs else ""))
srv.shutdown(); shutil.rmtree(CFG, ignore_errors=True)
print("\n" + ("ALL PASSED" if not fails else f"{fails} FAILED")); sys.exit(1 if fails else 0)
