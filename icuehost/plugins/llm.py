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
from collections import deque

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .base import PluginBase

HISTORY = 90          # samples kept for sparklines
MODELS_EVERY = 15     # seconds between /v1/models checks while /metrics works

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


def _header(reply: QNetworkReply, name: str) -> str:
    """rawHeader takes str in PySide6 >= 6.10, bytes before."""
    for arg in (name, name.encode()):
        try:
            return bytes(reply.rawHeader(arg)).decode("latin-1")
        except TypeError:
            continue
    return ""


def _set_header(req: QNetworkRequest, name: str, value: str) -> None:
    for args in ((name.encode(), value.encode()), (name, value)):
        try:
            req.setRawHeader(*args)
            return
        except TypeError:
            continue


class _Watch(QObject):
    def __init__(self, plugin: "LlmMonitorPlugin", base: str, interval_ms: int):
        super().__init__(plugin)
        self.p, self.base = plugin, base.rstrip("/")
        self.prev: tuple[float, float, float] | None = None     # t, gen, prompt
        self.history: deque = deque(maxlen=HISTORY)
        self.state: dict = {"url": self.base, "up": False, "models": [], "history": []}
        self.pending = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.set_interval(interval_ms)

    def set_interval(self, ms: int):
        self.timer.start(max(500, int(ms)))

    def _get(self, path: str, cb):
        req = QNetworkRequest(QUrl(self.base + path))
        req.setTransferTimeout(min(self.timer.interval(), 3000))
        _set_header(req, "Accept", "application/json, text/plain")
        t0 = time.monotonic()
        reply = self.p.nam.get(req)
        self.pending += 1

        def done():
            self.pending -= 1
            ok = reply.error() == QNetworkReply.NetworkError.NoError
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
            body = bytes(reply.readAll()).decode("utf-8", "replace")
            server = _header(reply, "Server")
            err = reply.errorString() if not ok else ""
            reply.deleteLater()
            try:
                cb(ok and 200 <= int(status) < 300, body, (time.monotonic() - t0) * 1000, err, server)
            finally:
                if self.pending == 0:
                    self.publish()
        reply.finished.connect(done)

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
        if gen is not None:
            now = time.monotonic()
            if self.prev:
                dt = now - self.prev[0]
                if dt > 0:
                    dg, dp = gen - self.prev[1], (prompt or 0) - self.prev[2]
                    if dg >= 0 and dp >= 0:          # counters reset on server restart
                        gen_tps, prompt_tps = dg / dt, (dp / dt if prompt is not None else None)
            self.prev = (now, gen, prompt or 0)
        else:
            self.prev = None
        st["genTps"], st["promptTps"] = gen_tps, prompt_tps
        self.history.append({"t": r["t"], "up": st["up"],
                             "gen": round(gen_tps, 2) if gen_tps is not None else None,
                             "prompt": round(prompt_tps, 2) if prompt_tps is not None else None})
        st["history"] = list(self.history)
        self.p.statsUpdated.emit(self.base, json.dumps(st))


class LlmMonitorPlugin(PluginBase):
    MODULE = "icuelinux.llmmonitor"

    statsUpdated = Signal(str, str)           # base url, JSON stats

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nam = QNetworkAccessManager(self)
        self.watches: dict[str, _Watch] = {}

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
            w.deleteLater()

    @Slot(int, str)
    def getStats(self, rid: int, url: str):
        w = self.watches.get(url.rstrip("/"))
        self.respond(rid, json.dumps(w.state) if w else "")
