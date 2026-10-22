"""Fake OpenAI-compatible LLM servers for testing the LLM Monitor widget.

  python tests/fake_llm_server.py vllm 18000      # vLLM-style /metrics
  python tests/fake_llm_server.py llamacpp 18001  # llama.cpp-style /metrics + /slots
  python tests/fake_llm_server.py plain 18002     # /v1/models only, no metrics

Simulates bursts of generation so counters, gauges and the sparkline move.
"""
import json
import math
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STYLE = sys.argv[1] if len(sys.argv) > 1 else "vllm"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18000
T0 = time.time()
MODEL = {"vllm": "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit", "llamacpp": "Qwen3-35B-A3B-Q4_K_M.gguf",
         "plain": "qwen3.8-flash-next", "burst": "halogen-qwen3.8-flash-next"}[STYLE]


def burst_state():
    """Halogen-like: one 6 s request every 10 s at 43 tok/s; the token counters
    only jump when a request completes."""
    t = time.time() - T0
    done, rem = divmod(t, 10)
    running = 1 if rem < 6 else 0
    completed = int(done) + (0 if rem < 6 else 1)
    return running, completed * 6 * 43.14, completed * 401


def state():
    """Deterministic load curve: busy ~70% of the time, rate varies."""
    t = time.time() - T0
    busy = (t % 20) < 14
    running = (2 + int(2 * math.sin(t / 3))) if busy else 0
    waiting = 1 if busy and (t % 20) > 10 else 0
    gen_rate = (45 + 25 * math.sin(t / 2.5)) if busy else 0
    # integral of the generation curve, so the counter is consistent with the rate
    cycles, rem = divmod(t, 20)
    def integ(x):
        x = min(x, 14)
        return 45 * x - 25 * 2.5 * (math.cos(x / 2.5) - 1)
    gen_total = cycles * integ(14) + integ(rem) + 1000
    prompt_total = gen_total * 3.2 + 5000
    kv = min(0.97, 0.15 + 0.2 * running + 0.05 * waiting)
    return running, waiting, gen_rate, gen_total, prompt_total, kv


class H(BaseHTTPRequestHandler):
    server_version = {"vllm": "uvicorn", "llamacpp": "llama.cpp", "plain": "halogen", "burst": "uvicorn"}[STYLE]

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        running, waiting, rate, gen, prompt, kv = state()
        if self.path == "/v1/models":
            return self.send(200, json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model"}]}))
        if self.path == "/metrics" and STYLE == "vllm":
            return self.send(200, "\n".join([
                "# HELP vllm:num_requests_running Number of requests in model execution batches.",
                "# TYPE vllm:num_requests_running gauge",
                f'vllm:num_requests_running{{engine="0",model_name="{MODEL}"}} {running}.0',
                "# TYPE vllm:num_requests_waiting gauge",
                f'vllm:num_requests_waiting{{engine="0",model_name="{MODEL}"}} {waiting}.0',
                "# TYPE vllm:kv_cache_usage_perc gauge",
                f'vllm:kv_cache_usage_perc{{engine="0",model_name="{MODEL}"}} {kv:.4f}',
                "# TYPE vllm:prompt_tokens_total counter",
                f'vllm:prompt_tokens_total{{engine="0",model_name="{MODEL}"}} {prompt:.1f}',
                "# TYPE vllm:generation_tokens_total counter",
                f'vllm:generation_tokens_total{{engine="0",model_name="{MODEL}"}} {gen:.1f}',
                f'vllm:generation_tokens_created{{engine="0",model_name="{MODEL}"}} {T0}',
                "# TYPE vllm:e2e_request_latency_seconds histogram",
                f'vllm:e2e_request_latency_seconds_bucket{{le="1.0",model_name="{MODEL}"}} 12.0',
                ""]), "text/plain; version=0.0.4")
        if self.path == "/metrics" and STYLE == "llamacpp":
            return self.send(200, "\n".join([
                "# TYPE llamacpp:prompt_tokens_total counter", f"llamacpp:prompt_tokens_total {int(prompt)}",
                "# TYPE llamacpp:tokens_predicted_total counter", f"llamacpp:tokens_predicted_total {int(gen)}",
                "# TYPE llamacpp:kv_cache_usage_ratio gauge", f"llamacpp:kv_cache_usage_ratio {kv:.3f}",
                "# TYPE llamacpp:requests_processing gauge", f"llamacpp:requests_processing {running}",
                "# TYPE llamacpp:requests_deferred gauge", f"llamacpp:requests_deferred {waiting}", ""]),
                "text/plain; version=0.0.4")
        if self.path == "/metrics" and STYLE == "burst":
            br, bg, bp = burst_state()
            return self.send(200, "\n".join([
                "# TYPE llamacpp:prompt_tokens_total counter", f"llamacpp:prompt_tokens_total {bp}",
                "# TYPE llamacpp:tokens_predicted_total counter", f"llamacpp:tokens_predicted_total {int(bg)}",
                "# TYPE llamacpp:predicted_tokens_seconds gauge", "llamacpp:predicted_tokens_seconds 43.14",
                "# TYPE llamacpp:requests_processing gauge", f"llamacpp:requests_processing {br}",
                "# TYPE llamacpp:requests_deferred gauge", "llamacpp:requests_deferred 0", ""]),
                "text/plain; version=0.0.4")
        if self.path == "/slots" and STYLE == "llamacpp":
            return self.send(200, json.dumps([{"id": i, "is_processing": i < running} for i in range(4)]))
        self.send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    print(f"fake {STYLE} server on :{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
