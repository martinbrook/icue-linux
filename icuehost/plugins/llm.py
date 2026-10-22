"""icuelinux.llmmonitor:LLM:1.0 — monitor OpenAI-compatible LLM servers.

A Linux-host extension (not an iCUE plugin): the host does the HTTP polling,
so widgets need no CORS support from the server and nothing changes on the
LLM machine.

Per watched base URL it polls:
  GET /v1/models   up/down, latency, loaded model ids        (any OpenAI-compatible server)
  GET /metrics     Prometheus counters/gauges if exposed      (vLLM, llama.cpp --metrics, SGLang, ...)
  GET /slots       llama.cpp per-slot activity, if exposed

and emits statsUpdated(url, json) with:
  up, latencyMs, models[], engine, hasMetrics,
  genTps, promptTps, running, waiting, kvUsage (0..1 or null),
  genTotal, promptTotal, history[{t, gen, prompt}], error
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .base import PluginBase

HISTORY = 90          # samples kept for sparklines
GEN_SECONDS = ("llamacpp:tokens_predicted_seconds_total",)
PROMPT_SECONDS = ("llamacpp:prompt_seconds_total",)


def _first(m: dict, names) -> float | None:
    return next((m[n] for n in names if n in m), None)
MODELS_EVERY = 15     # seconds between /v1/models checks while /metrics works
LIVE_STREAK = 3       # consecutive moving polls mid-request before a server counts as live

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEInfNa]+)')


def parse_prometheus(text: str) -> dict[str, float]:
    """Sum each metric family across its label sets (one engine, many models
    or GPUs -> one number). Histograms' _bucket lines are skipped."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line)
        if not m or m.group(1).endswith("_bucket"):
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v:            # NaN
            continue
        out[m.group(1)] = out.get(m.group(1), 0.0) + v
    return out


# Metric names by meaning. Exact names first; then a suffix match so engines
# that copy vLLM/llama.cpp naming under their own prefix are picked up too.
FAMILIES = {
    "gen_total": ["vllm:generation_tokens_total", "llamacpp:tokens_predicted_total",
                  "sglang:generation_tokens_total", "tgi_request_generated_tokens_sum"],
    "prompt_total": ["vllm:prompt_tokens_total", "llamacpp:prompt_tokens_total",
                     "sglang:prompt_tokens_total", "tgi_request_input_length_sum"],
    "running": ["vllm:num_requests_running", "llamacpp:requests_processing",
                "sglang:num_running_reqs", "tgi_batch_current_size"],
    "waiting": ["vllm:num_requests_waiting", "llamacpp:requests_deferred",
                "sglang:num_queue_reqs", "tgi_queue_size"],
    "kv": ["vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc",
           "llamacpp:kv_cache_usage_ratio", "sglang:token_usage"],
}
SUFFIXES = {
    "gen_total": ["generation_tokens_total", "tokens_predicted_total", "output_tokens_total",
                  "completion_tokens_total", "generated_tokens_total"],
    "prompt_total": ["prompt_tokens_total", "input_tokens_total", "prompt_tokens_processed_total"],
    "running": ["num_requests_running", "requests_processing", "running_requests", "num_running_reqs"],
    "waiting": ["num_requests_waiting", "requests_deferred", "waiting_requests", "num_queue_reqs",
                "queue_size"],
    "kv": ["kv_cache_usage_perc", "gpu_cache_usage_perc", "kv_cache_usage_ratio",
           "kv_cache_usage", "token_usage"],
}


def pick(metrics: dict[str, float], key: str) -> tuple[str | None, float | None]:
    for name in FAMILIES[key]:
        if name in metrics:
            return name, metrics[name]
    for suf in SUFFIXES[key]:
        for name, v in metrics.items():
            if name.endswith(suf) and not name.endswith("_created"):
                return name, v
    return None, None


def engine_of(metrics: dict[str, float], server_header: str) -> str:
    for prefix, label in (("vllm:", "vLLM"), ("llamacpp:", "llama.cpp"),
                          ("sglang:", "SGLang"), ("tgi_", "TGI")):
        if any(k.startswith(prefix) for k in metrics):
            return label
    for k in metrics:
        if ":" in k:
            return k.split(":")[0]
    # "halogen Python/3.13" -> "halogen"; "llama.cpp" stays
    return (server_header or "").split(" ")[0].split("/")[0]


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # LAN servers: no proxy


def _fetch(url: str, timeout: float):
    """(ok, body, ms, error, server) for one GET. Runs on a worker thread."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"Accept": "application/json, text/plain"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(4 << 20).decode("utf-8", "replace")
            return (200 <= resp.status < 300, body, (time.monotonic() - t0) * 1000, "",
                    resp.headers.get("Server", ""))
    except urllib.error.HTTPError as e:
        return False, "", (time.monotonic() - t0) * 1000, f"HTTP {e.code}", e.headers.get("Server", "")
    except urllib.error.URLError as e:
        reason = e.reason
        msg = getattr(reason, "strerror", None) or str(reason)
        if isinstance(reason, ConnectionRefusedError):
            msg = "Connection refused"
        elif "timed out" in str(reason):
            msg = "timed out"
        return False, "", (time.monotonic() - t0) * 1000, msg, ""
    except (OSError, ValueError) as e:     # socket timeouts, bad URLs
        return False, "", (time.monotonic() - t0) * 1000, "timed out" if "timed out" in str(e) else str(e), ""


def _fetch_job(plugin, watch, url, timeout, cb):
    result = _fetch(url, timeout)
    plugin._fetched.emit((watch, cb, result))     # queued to the Qt thread


class _Watch(QObject):
    def __init__(self, plugin: "LlmMonitorPlugin", base: str, interval_ms: int):
        super().__init__(plugin)
        self.p, self.base = plugin, base.rstrip("/")
        self.prev: tuple[float, float, float] | None = None     # t, gen, prompt
        self.history: deque = deque(maxlen=HISTORY)
        self.state: dict = {"url": self.base, "up": False, "models": [], "history": []}
        # Servers like llama.cpp (and Halogen) only add a request's tokens to the
        # counters when it finishes. Until a counter is seen moving mid-request we
        # treat jumps as "request finished": no spike, the request's rate instead.
        self.live = False
        self.move_streak = self.still_streak = 0
        self.prev_running = 0
        self.busy_since: float | None = None     # wall-clock time the current request started
        self.last_tps: float | None = None
        self.last_prompt_tps: float | None = None
        self.prev_secs: tuple[float | None, float | None] = (None, None)   # predict, prompt seconds
        self.pending = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.set_interval(interval_ms)

    def set_interval(self, ms: int):
        self.timer.start(max(500, int(ms)))

    def _get(self, path: str, cb):
        """Fetch base+path on a worker thread; cb runs on the Qt thread.
        Plain urllib instead of QNetworkAccessManager: no QNetworkReply objects
        whose C++/Python lifetimes can disagree (a double free showed up as
        'free(): invalid pointer' during garbage collection)."""
        self.pending += 1
        timeout = min(self.timer.interval(), 3000) / 1000
        self.p.pool.submit(_fetch_job, self.p, self, self.base + path, timeout, cb)

    def poll(self):
        if self.pending:            # previous round still in flight
            return
        self.round = {"t": time.time()}
        # Once /metrics is known to work it doubles as the health check, so the
        # model list is only refreshed every MODELS_EVERY seconds (keeps the
        # server's access log quieter).
        now = time.monotonic()
        if (not self.state.get("hasMetrics") or not self.state.get("up")
                or now - getattr(self, "models_at", 0) >= MODELS_EVERY):
            self.models_at = now
            self._get("/v1/models", self._models)
        self._get("/metrics", self._metrics)
        if self.state.get("hasSlots", True):
            self._get("/slots", self._slots)

    # ------------------------------------------------------------- handlers
    def _models(self, ok, body, ms, err, server):
        r = self.round
        r["up"], r["latencyMs"], r["server"] = ok, round(ms), server
        if ok:
            try:
                data = json.loads(body)
                items = data.get("data", data.get("models", []))
                r["models"] = [m.get("id") or m.get("name") or m.get("model") for m in items
                               if isinstance(m, dict)]
            except (ValueError, AttributeError):
                r["models"] = []
        else:
            r["error"] = err or "no response"

    def _metrics(self, ok, body, ms, err, server):
        r = self.round
        r["metricsOk"], r["metricsMs"], r["metricsErr"] = ok, round(ms), err
        r.setdefault("server", server)
        if ok and ("# TYPE" in body or "# HELP" in body or _LINE.search(body)):
            r["metrics"] = parse_prometheus(body)

    def _slots(self, ok, body, ms, err, server):
        if not ok:
            self.state["hasSlots"] = False      # don't keep asking
            return
        try:
            slots = json.loads(body)
            self.round["slotsBusy"] = sum(1 for s in slots if s.get("is_processing"))
            self.round["slotsTotal"] = len(slots)
        except (ValueError, AttributeError, TypeError):
            self.state["hasSlots"] = False

    def _sec_deltas(self, m):
        """Seconds the server spent generating / processing prompts since the last poll."""
        out = []
        for names, prev in ((GEN_SECONDS, self.prev_secs[0]), (PROMPT_SECONDS, self.prev_secs[1])):
            cur = _first(m, names)
            out.append(cur - prev if cur is not None and prev is not None and cur >= prev else None)
        return tuple(out)

    def _burst(self, st, m, r, dg, dp, dt, running, secs=(None, None)):
        """Rates for servers whose counters only move when a request completes."""
        now = r["t"]
        if running > 0 and self.busy_since is None:
            self.busy_since = now - dt / 2
        if dg <= 0:
            idle = 0.0 if running == 0 else None      # unknown while a request runs
            return idle, idle
        # a request just finished: work out its rate
        # Best: tokens / the server's own generation time for those tokens (exact,
        # and unaffected by other scrapers). Then the per-scrape gauge, then wall time.
        gen_secs, prompt_secs = secs
        metric = next((m[k] for k in ("llamacpp:predicted_tokens_seconds", "predicted_tokens_seconds")
                       if m.get(k)), None)
        duration = now - self.busy_since if self.busy_since is not None else None
        if gen_secs and gen_secs > 0:
            rate = dg / gen_secs
        elif metric:
            rate = metric
        elif duration and duration > 0.5:
            rate = dg / duration
        else:
            rate = None                               # too short to measure honestly
        if rate is not None:
            self.last_tps = rate
            if self.busy_since is not None:           # draw the request at its rate
                for pt in self.history:
                    if pt["t"] >= self.busy_since:
                        pt["gen"] = round(rate, 2)
        self.busy_since = now if running > 0 else None
        if dp > 0 and prompt_secs and prompt_secs > 0:
            ptps = dp / prompt_secs
        else:
            ptps = next((m[k] for k in ("llamacpp:prompt_tokens_seconds", "prompt_tokens_seconds")
                         if m.get(k)), None)
        if ptps is not None:
            self.last_prompt_tps = ptps
        return rate, ptps

    # --------------------------------------------------------------- publish
    def publish(self):
        r, st = self.round, self.state
        m = r.get("metrics") or {}
        if "up" not in r:                       # models skipped this round: /metrics is the health check
            r["up"], r["latencyMs"] = r.get("metricsOk", False), r.get("metricsMs")
            if not r["up"]:
                r["error"] = r.get("metricsErr") or "no response"
        st.update(up=bool(r.get("up")), latencyMs=r.get("latencyMs"), error=r.get("error", ""),
                  hasMetrics=bool(m), t=r["t"])
        if r.get("up") and "models" in r:
            st["models"] = r["models"]
        st["engine"] = engine_of(m, r.get("server", "")) if (m or r.get("server")) else st.get("engine", "")

        _, gen = pick(m, "gen_total")
        _, prompt = pick(m, "prompt_total")
        _, running = pick(m, "running")
        _, waiting = pick(m, "waiting")
        _, kv = pick(m, "kv")
        if running is None and "slotsBusy" in r:
            running = r["slotsBusy"]
        st.update(running=running, waiting=waiting,
                  kvUsage=(kv / 100 if kv is not None and kv > 1.0 else kv),
                  genTotal=gen, promptTotal=prompt,
                  slotsTotal=r.get("slotsTotal"))

        gen_tps = prompt_tps = None
        running_now = running or 0
        busy_from_slots = r.get("slotsBusy")
        if busy_from_slots is not None:
            running_now = max(running_now, busy_from_slots)
        now = time.monotonic()
        point = {"t": r["t"], "up": st["up"], "gen": None, "prompt": None}
        if gen is not None and self.prev:
            dt = now - self.prev[0]
            dg, dp = gen - self.prev[1], (prompt or 0) - self.prev[2]
            if dt > 0 and dg >= 0 and dp >= 0:        # counters reset on server restart
                # Live servers move their counters on every poll while generating.
                # End-of-request servers only jump when a request completes, which
                # can also happen mid-busy-period when an agent sends requests back
                # to back, so require a streak before believing it's live, and drop
                # back if the counter then sits still during a request.
                if running_now > 0 and self.prev_running > 0:
                    if dg > 0:
                        self.move_streak += 1
                        self.still_streak = 0
                    else:
                        self.still_streak += 1
                        self.move_streak = 0
                    if self.move_streak >= LIVE_STREAK:
                        self.live = True
                    elif self.still_streak >= LIVE_STREAK and self.live:
                        self.live = False
                        self.busy_since = None
                if self.live:
                    gen_tps = dg / dt
                    prompt_tps = dp / dt if prompt is not None else None
                else:
                    gen_tps, prompt_tps = self._burst(st, m, r, dg, dp, dt, running_now,
                                                      self._sec_deltas(m))
        self.prev_secs = (_first(m, GEN_SECONDS), _first(m, PROMPT_SECONDS))
        if gen is not None:
            self.prev = (now, gen, prompt or 0)
        else:
            self.prev = None
        self.prev_running = running_now
        st["genTps"], st["promptTps"], st["liveRate"] = gen_tps, prompt_tps, self.live
        st["lastTps"], st["busy"] = self.last_tps, running_now > 0
        st["lastPromptTps"] = self.last_prompt_tps
        point["gen"] = round(gen_tps, 2) if gen_tps is not None else None
        point["prompt"] = round(prompt_tps, 2) if prompt_tps is not None else None
        self.history.append(point)
        st["history"] = list(self.history)
        self.p.statsUpdated.emit(self.base, json.dumps(st))




class LlmMonitorPlugin(PluginBase):
    MODULE = "icuelinux.llmmonitor"

    statsUpdated = Signal(str, str)           # base url, JSON stats

    _fetched = Signal(object)                 # (watch, callback, result) from worker threads

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-poll")
        self._fetched.connect(self._deliver)
        self.watches: dict[str, _Watch] = {}

    def _deliver(self, item):
        watch, cb, (ok, body, ms, err, server) = item
        watch.pending -= 1
        if watch not in self.watches.values():        # unwatched meanwhile
            return
        try:
            cb(ok, body, ms, err, server)
        finally:
            if watch.pending == 0:
                watch.publish()

    @Slot(str, int)
    def watch(self, url: str, interval_ms: int = 2000):
        """Start (or retune) polling of a server base URL, e.g. http://corsair:8731"""
        base = url.strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        if not base:
            return
        if not re.match(r"^https?://", base):
            base = "http://" + base
        w = self.watches.get(base)
        if w:
            w.set_interval(interval_ms)
        else:
            self.watches[base] = w = _Watch(self, base, interval_ms)
        QTimer.singleShot(0, w.poll)

    @Slot(str)
    def unwatch(self, url: str):
        w = self.watches.pop(url.rstrip("/"), None)
        if w:
            w.timer.stop()
            w.setParent(None)      # Python owns it now; freed normally when unreferenced

    @Slot(int, str)
    def getStats(self, rid: int, url: str):
        w = self.watches.get(url.rstrip("/"))
        self.respond(rid, json.dumps(w.state) if w else "")
