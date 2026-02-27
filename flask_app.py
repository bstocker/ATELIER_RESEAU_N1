import time
import random
import sqlite3
from collections import deque
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

# ---- Simple in-memory "QoS" simulation state ----
WINDOW = deque(maxlen=2000)  # store (t, endpoint, duration_ms, status)
TOKENS_PER_SEC = 5           # rate limit (simulated QoS policy)
BURST = 10
tokens = BURST
last_refill = time.time()

DB_PATH = "/home/yourusername/networklab.db"  # optional if you want persistence

def now_ms():
    return int(time.time() * 1000)

def refill_tokens():
    global tokens, last_refill
    t = time.time()
    elapsed = t - last_refill
    add = int(elapsed * TOKENS_PER_SEC)
    if add > 0:
        tokens = min(BURST, tokens + add)
        last_refill = t

def qos_admit():
    """Token bucket admission: return (allowed, retry_after_seconds)."""
    global tokens
    refill_tokens()
    if tokens > 0:
        tokens -= 1
        return True, 0
    return False, 1  # simplistic retry-after

def record(endpoint, duration_ms, status):
    WINDOW.append((time.time(), endpoint, duration_ms, status))

def compute_metrics():
    # Compute latency percentiles + error rate + throughput approximation over last N entries
    data = list(WINDOW)
    if not data:
        return {}
    durations = sorted([d[2] for d in data])
    errors = sum(1 for d in data if d[3] >= 400)
    total = len(data)
    error_rate = errors / total

    # Approx throughput over last 60 seconds
    cutoff = time.time() - 60
    last60 = [d for d in data if d[0] >= cutoff]
    rps = len(last60) / 60 if last60 else 0

    def pct(p):
        idx = int(round((p/100) * (len(durations)-1)))
        return durations[max(0, min(idx, len(durations)-1))]

    # jitter = stddev-ish approximation on consecutive diffs (simple)
    diffs = [abs(durations[i] - durations[i-1]) for i in range(1, len(durations))]
    jitter = sum(diffs)/len(diffs) if diffs else 0

    return {
        "count": total,
        "error_rate": round(error_rate, 4),
        "rps_last_60s": round(rps, 3),
        "latency_ms": {
            "p50": pct(50),
            "p90": pct(90),
            "p95": pct(95),
            "p99": pct(99),
            "max": durations[-1],
        },
        "jitter_ms_avg_absdiff": round(jitter, 2),
        "qos_policy": {
            "token_bucket": {"tokens_per_sec": TOKENS_PER_SEC, "burst": BURST}
        }
    }

@app.before_request
def start_timer():
    request._t0 = time.time()

@app.after_request
def end_timer(response):
    duration_ms = int((time.time() - getattr(request, "_t0", time.time())) * 1000)
    record(request.path, duration_ms, response.status_code)
    # Expose "service" metadata (contract-ish headers)
    response.headers["X-Service-Name"] = "network-lab"
    response.headers["X-Service-Version"] = "1.0"
    response.headers["X-Request-Id"] = str(now_ms()) + "-" + str(random.randint(1000,9999))
    return response

@app.get("/")
def index():
    return """
    <h1>Network Lab (Flask)</h1>
    <ul>
      <li><a href="/osi">/osi</a> — OSI mapping</li>
      <li><a href="/service">/service</a> — service contract & dependencies</li>
      <li><a href="/slow?ms=300">/slow</a> — simulate latency</li>
      <li><a href="/loss?p=0.2">/loss</a> — simulate errors/loss</li>
      <li><a href="/qos">/qos</a> — live QoS metrics</li>
      <li><a href="/qos/reset">/qos/reset</a> — reset window</li>
    </ul>
    """

@app.get("/osi")
def osi():
    # What we can observe from an app (mostly L7/L4, hints of lower layers)
    info = {
        "L7_Application": {
            "http_method": request.method,
            "path": request.path,
            "headers_subset": {k: request.headers.get(k) for k in ["Host","User-Agent","Accept","Content-Type"]}
        },
        "L6_Presentation": "Often handled by libraries: JSON (UTF-8), TLS details not exposed here",
        "L5_Session": "HTTP keep-alive / cookies; session mgmt at app/framework level",
        "L4_Transport": {
            "remote_addr": request.remote_addr,
            "note": "TCP used under the hood for HTTP(S). Ports/handshake not directly visible in Flask."
        },
        "L3_Network": "IP routing happens below; we see the client IP (possibly via proxy).",
        "L2_DataLink": "Ethernet/Wi-Fi frames not visible to app in PaaS.",
        "L1_Physical": "Cables/radio not visible to app."
    }
    return jsonify(info)

@app.get("/service")
def service():
    # Service = interface + contract + dependencies + SLO targets
    contract = {
        "service": "network-lab",
        "version": "1.0",
        "endpoints": [
            {"path": "/slow", "method": "GET", "params": {"ms": "int (simulated processing time)"}},
            {"path": "/loss", "method": "GET", "params": {"p": "float in [0,1] (error probability)"}},
            {"path": "/qos", "method": "GET", "desc": "QoS metrics computed server-side"},
        ],
        "dependencies": [
            {"type": "runtime", "name": "PythonAnywhere WSGI + Flask"},
            {"type": "network", "name": "Internet / DNS / TLS termination (platform-managed)"}
        ],
        "slo_examples": {
            "availability": "99.5% monthly (example)",
            "latency_p95_ms": 400,
            "error_rate": "< 1%"
        }
    }
    return jsonify(contract)

@app.get("/slow")
def slow():
    allowed, retry_after = qos_admit()
    if not allowed:
        resp = make_response(jsonify({"error": "rate_limited", "retry_after_s": retry_after}), 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    ms = int(request.args.get("ms", "200"))
    time.sleep(ms / 1000)
    return jsonify({"ok": True, "simulated_processing_ms": ms})

@app.get("/loss")
def loss():
    allowed, retry_after = qos_admit()
    if not allowed:
        resp = make_response(jsonify({"error": "rate_limited", "retry_after_s": retry_after}), 429)
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    p = float(request.args.get("p", "0.1"))
    if random.random() < p:
        return jsonify({"ok": False, "simulated": "loss/error"}), 503
    return jsonify({"ok": True, "simulated": "no_loss"})

@app.get("/qos")
def qos():
    return jsonify(compute_metrics())

@app.get("/qos/reset")
def qos_reset():
    WINDOW.clear()
    return jsonify({"ok": True, "message": "metrics window cleared"})

if __name__ == "__main__":
    # utile en local uniquement
    app.run(host="0.0.0.0", port=5000, debug=True)
