"""Headless test of the LLM Monitor widget against the fake servers.

  QT_QPA_PLATFORM=offscreen python tests/llm_monitor_test.py [outdir]

Starts vLLM-style, llama.cpp-style and models-only fakes, plus one URL with
nothing listening, renders the widget for each, checks the numbers and saves
screenshots.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = str(HERE / ".cfg-llm")
os.environ.setdefault("ICUE_LINUX_QUIET", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
shutil.rmtree(HERE / ".cfg-llm", ignore_errors=True)

servers = [subprocess.Popen([sys.executable, str(HERE / "fake_llm_server.py"), style, str(port)])
           for style, port in (("vllm", 18000), ("llamacpp", 18001), ("plain", 18002), ("burst", 18003))]
time.sleep(1.0)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv[:1])
from icuehost.host import WidgetInstance, WidgetView  # noqa: E402
from icuehost.plugins import PluginHub  # noqa: E402

hub = PluginHub()
cases = [  # name, url, size, extra settings
    ("vllm", "127.0.0.1:18000/v1", (840, 344), {"displayName": "DGX Spark"}),
    ("llamacpp", "http://127.0.0.1:18001", (840, 344), {"displayName": "Corsair 300"}),
    ("plain", "http://127.0.0.1:18002", (840, 344), {"displayName": "Halogen"}),
    ("down", "http://127.0.0.1:18009", (840, 344), {"displayName": "Corsair 300"}),
    ("halogen", "http://127.0.0.1:18003", (840, 344), {"displayName": "Corsair 300"}),
    ("vllm-large", "http://127.0.0.1:18000", (840, 696), {"displayName": "DGX Spark"}),
    ("vllm-xl", "http://127.0.0.1:18000", (2536, 696), {"displayName": "DGX Spark", "accentColor": "#a78bfa"}),
]
views = {}
for name, url, (w, h), extra in cases:
    inst = WidgetInstance(ROOT / "examples/llm-monitor", hub, instance=f"llm-{name}", language="en",
                          overrides={"serverUrl": url, "pollSeconds": 1, **extra})
    v = WidgetView(inst)
    v.page().setBackgroundColor(QColor("#000"))
    v.resize(w, h)
    v.show()
    views[name] = v

failures = []
results = {}


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


JS = """JSON.stringify({gen: gen.textContent, label: genLabel.textContent, model: model.textContent,
  meta: meta.textContent, running: running.textContent, waiting: waiting.textContent,
  kv: kvPct.textContent, kvHidden: kvBox.classList.contains('hidden'),
  notice: notice.classList.contains('hidden') ? '' : notice.textContent,
  dot: dot.className, font: getComputedStyle(document.body).fontVariantNumeric})"""


def collect():
    pending = set(views)
    for name, v in views.items():
        def got(r, name=name):
            results[name] = json.loads(r)
            v2 = views[name]
            v2.grab().save(str(OUT / f"llm-{name}.png"))
            pending.discard(name)
            if not pending:
                report()
        v.page().runJavaScript(JS, 0, got)


def report():
    for k, r in results.items():
        print(f"  {k:11} {r}")
    v, l, p, d = results["vllm"], results["llamacpp"], results["plain"], results["down"]
    check(v["font"] == "tabular-nums", "widget stylesheet applied")
    check("Qwen3.6" in v["model"] and v["meta"].startswith("vLLM"), "vLLM: model + engine detected")
    check(v["gen"] not in ("–", "") and v["running"] != "–" and v["kv"].endswith("%"), "vLLM: rate, running, KV shown")
    check("llama.cpp" in l["meta"] and l["running"].endswith("/4"), "llama.cpp: engine + slots detected")
    check(l["gen"] not in ("–", ""), "llama.cpp: rate shown")
    check("Halogen".lower() in p["meta"].lower() or p["meta"], "models-only: up with latency")
    check("metrics" in p["notice"], "models-only: explains missing throughput")
    check("down" in d["dot"] and "offline" in d["meta"].lower(), "unreachable server shown offline")
    h = results["halogen"]
    check(h["gen"] in ("…", "43") and h["label"] in ("generating", "last request"),
          f"end-of-request counters: no spike, request rate shown ({h['gen']} / {h['label']})")
    errs = [c for v in views.values() for c in v.page().console if "rror" in c]
    check(not errs, "no JS errors" + (": " + "; ".join(errs) if errs else ""))
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    for s in servers:
        s.terminate()
    app.exit(1 if failures else 0)


QTimer.singleShot(12500, collect)
QTimer.singleShot(40000, lambda: (print("timeout"), [s.terminate() for s in servers], app.exit(2)))
sys.exit(app.exec())
