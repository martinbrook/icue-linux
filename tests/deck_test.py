"""Headless test of the Deck widget with fake system tools.

  QT_QPA_PLATFORM=offscreen python tests/deck_test.py [outdir]
"""
import json, os, shutil, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
STATE = HERE / ".deck-state"
shutil.rmtree(STATE, ignore_errors=True); STATE.mkdir()
shutil.rmtree(HERE / ".cfg-deck", ignore_errors=True)
os.environ.update({"QT_QPA_PLATFORM": "offscreen", "XDG_CONFIG_HOME": str(HERE / ".cfg-deck"),
                   "ICUE_LINUX_QUIET": "1", "FAKE_STATE": str(STATE),
                   "PATH": f"{HERE / 'fakebin'}:{os.environ['PATH']}", "XDG_SESSION_TYPE": "x11",
                   "XDG_DATA_DIRS": str(STATE / "share")})
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
sys.path.insert(0, str(ROOT))
(STATE / "share/applications").mkdir(parents=True)
(STATE / "share/applications/firefox.desktop").write_text("[Desktop Entry]\nName=Firefox\nExec=firefox\n")
(STATE / "share/applications/org.gnome.Terminal.desktop").write_text("[Desktop Entry]\nName=Terminal\nExec=gnome-terminal\n")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
img = QImage(300, 300, QImage.Format.Format_RGB32)
p = QPainter(img); g = QLinearGradient(0, 0, 300, 300); g.setColorAt(0, QColor("#ff6a3d")); g.setColorAt(1, QColor("#5b2a86"))
p.fillRect(img.rect(), g); p.end(); img.save(str(STATE / "art.png"))
# serve the art over http, like Spotify's CDN, to prove widgets can load web images
import subprocess
ART_SRV = subprocess.Popen([sys.executable, "-m", "http.server", "18077", "--bind", "127.0.0.1"],
                           cwd=STATE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
time.sleep(0.8)
os.environ["FAKE_ART"] = "http://127.0.0.1:18077/art.png"

from icuehost.host import WidgetInstance, WidgetView
from icuehost.plugins import PluginHub
hub = PluginHub()
PRESSES = []
hub.get("icuelinux.deck").pressed.connect(lambda prof, r: PRESSES.append(json.loads(r)))
views = {}
for name, (w, h) in {"small": (840, 344), "xl": (2536, 696), "large": (840, 696)}.items():
    v = WidgetView(WidgetInstance(ROOT / "examples/deck", hub, instance=f"deck-{name}"))
    v.page().setBackgroundColor(QColor("#000")); v.resize(w, h); v.show(); views[name] = v
small = views["small"]
deck_file = HERE / ".cfg-deck/icue-linux/deck.toml"
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)
def calls(): return (STATE / "calls").read_text() if (STATE / "calls").exists() else ""
def js(code, cb):
    def safe(r):
        try:
            cb(r)
        except Exception as e:          # never let a test assertion raise inside Qt
            check(False, f"callback error: {e!r} for {r!r}")
    small.page().runJavaScript(code, 0, safe)

TAP = """(function(i){const b=document.querySelectorAll('#grid .btn')[i]; const r=b.getBoundingClientRect();
 const o={bubbles:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2,pointerId:1,isPrimary:true};
 b.dispatchEvent(new PointerEvent('pointerdown',o)); b.dispatchEvent(new PointerEvent('pointerup',o)); return b.textContent.trim().slice(0,40);})(%d)"""
STATE_JS = """(function(){try{return JSON.stringify({n: document.querySelectorAll('#grid .btn').length, tab: (document.querySelector('.tab.on')||{}).textContent,
 np: (document.querySelector('.np .t')||{}).textContent, art: (document.querySelector('.np .art')||{style:{}}).style.backgroundImage,
 css: getComputedStyle(document.body).userSelect,
 artBox: (()=>{const a=document.querySelector('.np .art'); if(!a) return null; const r=a.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)];})(),
 playIcon: (document.querySelectorAll('#grid .btn')[2]||{innerHTML:''}).innerHTML.includes('rect'), toast: document.getElementById('toast').classList.contains('show') ? document.getElementById('toast').textContent : '',
 labels: [...document.querySelectorAll('#grid .lbl')].map(e=>e.textContent), cols: document.getElementById('grid').style.gridTemplateColumns})}catch(e){return JSON.stringify({err: String(e)})}})()"""

steps = []
def step(delay):
    def deco(f): steps.append((delay, f)); return f
    return deco

@step(4500)
def s1():
    for n, v in views.items(): v.grab().save(str(OUT / f"deck-{n}.png"))
    js(STATE_JS, lambda r: (print("  initial", r), r1(json.loads(r))))
def r1(r):
    check(r["n"] == 10 and r["tab"] == "Main", "Main page rendered with 10 buttons")
    check(r["np"] == "Wandering Signal" and "http://127.0.0.1:18077/art.png" in r["art"], "now-playing tile loads album art from the web")
    check(r["css"] == "none", "widget stylesheet applied alongside web access")
    check(r["artBox"] and abs(r["artBox"][0] - r["artBox"][1]) <= 1, f"album art is square {r['artBox']}")
    check(r["playIcon"], "play button shows pause icon while playing")
    check(deck_file.exists(), "default deck.toml created from example")

@step(800)
def s2(): js(TAP % 5, lambda t: print("  tapped", t))            # Firefox (app)
@step(400)
def s3(): js(TAP % 4, lambda t: print("  tapped", t))            # Terminal (app)
@step(400)
def s4(): js(TAP % 2, lambda t: print("  tapped", t))            # play-pause
@step(1500)
def s5():
    c = calls(); print("  calls:", c.strip().splitlines()[-6:])
    check("gtk-launch firefox" in c, "app button launches via gtk-launch")
    check("gtk-launch org.gnome.Terminal" in c, "terminal button launches the terminal app")
    check("playerctl -p spotify play-pause" in c, "spotify button calls playerctl")
    js(STATE_JS, lambda r: check(not json.loads(r)["playIcon"], "play icon flips after pause (mediaChanged)"))
@step(500)
def s6(): js(TAP % 9, lambda t: print("  tapped", t))            # More -> page Tools
@step(1500)
def s7():
    js(STATE_JS, lambda r: check(json.loads(r)["tab"] == "Tools", "page button switches page"))
    small.grab().save(str(OUT / "deck-tools.png"))
@step(300)
def s7b(): js(TAP % 4, lambda t: print("  tapped", t))           # Tools: Copy (keys ctrl+c)
@step(1200)
def s7c(): check("xdotool key --clearmodifiers ctrl+c" in calls(), "keys button sends chord via xdotool")
@step(300)
def s8():   # live reload + failing action
    t = deck_file.read_text().replace('label = "Back"', 'label = "Home"')
    t += '\n  [[page.button]]\n  label = "Broken"\n  command = ["no-such-program-xyz"]\n'
    deck_file.write_text(t)
@step(1500)
def s9(): js(STATE_JS, lambda r: check("Home" in json.loads(r)["labels"] and "Broken" in json.loads(r)["labels"], "deck.toml edits reload live"))
@step(300)
def s10(): js(TAP % 8, lambda t: print("  tapped", t))
@step(1500)
def s11():
    print("  presses:", PRESSES[-2:])
    js(STATE_JS, lambda r: check("no-such-program-xyz" in json.loads(r)["toast"], "failed action shows a message: " + json.loads(r)["toast"]))
    small.grab().save(str(OUT / "deck-error.png"))
@step(1500)
def done():
    errs = [c for v in views.values() for c in v.page().console if "rror" in c]
    check(not errs, "no JS errors" + (": " + "; ".join(errs) if errs else ""))
    ART_SRV.terminate()
    print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED"))
    app.exit(1 if fails else 0)

t = 0
for d, f in steps:
    t += d; QTimer.singleShot(t, f)
QTimer.singleShot(t + 15000, lambda: (print("timeout"), app.exit(2)))
sys.exit(app.exec())
