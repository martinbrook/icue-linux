"""Rate logic against end-of-request servers (Halogen-style), incl. agent loops
that send requests back to back.   python tests/llm_rates_test.py"""
import json, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from PySide6.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
from icuehost.plugins.llm import LlmMonitorPlugin
srv = {s: subprocess.Popen([sys.executable, str(HERE / "fake_llm_server.py"), s, str(p)])
       for s, p in (("agent", 18071), ("burst", 18072), ("vllm", 18073))}
time.sleep(1)
p = LlmMonitorPlugin(); last = {}
p.statsUpdated.connect(lambda u, j: last.__setitem__(u, json.loads(j)))
for port in (18071, 18072, 18073):
    p.watch(f"http://127.0.0.1:{port}", 1000)
QTimer.singleShot(16500, app.quit); app.exec()
for s in srv.values(): s.terminate()
fails = 0
def check(c, m):
    global fails; print(("PASS " if c else "FAIL ") + m); fails += (not c)
a, b, v = (last[f"http://127.0.0.1:{p}"] for p in (18071, 18072, 18073))
peak = max((x["gen"] or 0) for x in a["history"])
check(not a["liveRate"] and 44 < (a["lastTps"] or 0) < 46, f"agent loop: end-of-request mode, rate {a['lastTps']}")
check(peak < 50, f"agent loop: no spike in sparkline (peak {peak})")
check(not b["liveRate"] and 44 < (b["lastTps"] or 0) < 46, f"single requests: rate {b['lastTps']}")
check(v["liveRate"], "streaming server (vLLM) detected as live")
print("\n" + ("ALL PASSED" if not fails else f"{fails} FAILED")); sys.exit(1 if fails else 0)
