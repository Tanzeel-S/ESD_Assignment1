#!/usr/bin/env python3
"""Fixed-rate (open-loop) load generator for Assignment 3.

Sends HTTP GET /products/{id} and/or gRPC GetProduct through Envoy at a fixed
rate for a fixed duration. Each request is scheduled at start + i/rate and sent
without waiting for earlier responses. No retries, no circuit breaker.

    docker compose run --rm tools python scripts/loadtest.py --rate 20 --duration 60 --label round-robin

Results (rate, sent, successes, failures, client p50/p95, replica shares, and a
per-second timeline) are printed and saved to results/<label>.json.
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grpc  # noqa: E402

from grpc_service import checkout_pb2, checkout_pb2_grpc

API_URL = os.getenv("API_URL", "http://envoy:8000")
GRPC_TARGET = os.getenv("GRPC_TARGET", "envoy:50051")
TIMEOUT = 5.0


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.results = []  # (offset_s, latency_s, ok, status, replica, request_id)

    def add(self, *row):
        with self.lock:
            self.results.append(row)


def make_product():
    body = json.dumps({"name": "Load test item", "price_cents": 999, "stock": 100}).encode()
    req = urllib.request.Request(
        f"{API_URL}/products", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-HomeShop-User": "loadgen"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())["id"]


def http_call(product_id, t0, rec):
    request_id = str(uuid.uuid4())
    sent = time.monotonic()
    req = urllib.request.Request(f"{API_URL}/products/{product_id}", headers={"X-Request-ID": request_id})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
            rec.add(sent - t0, time.monotonic() - sent, True, str(resp.status), resp.headers.get("X-Replica", "-"), request_id)
    except urllib.error.HTTPError as err:
        rec.add(sent - t0, time.monotonic() - sent, False, str(err.code), err.headers.get("X-Replica", "-"), request_id)
    except Exception as err:  # timeout / connection error
        rec.add(sent - t0, time.monotonic() - sent, False, type(err).__name__, "-", request_id)


def run_http(product_id, rate, duration, rec, t0):
    with ThreadPoolExecutor(max_workers=256) as pool:
        for i in range(int(rate * duration)):
            delay = t0 + i / rate - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pool.submit(http_call, product_id, t0, rec)


def run_grpc(product_id, rate, duration, rec, t0):
    # One channel (one HTTP/2 connection) to Envoy; transparent retries disabled.
    channel = grpc.insecure_channel(GRPC_TARGET, options=[("grpc.enable_retries", 0)])
    stub = checkout_pb2_grpc.CheckoutServiceStub(channel)
    pending = []

    def done(future, sent, request_id):
        latency = time.monotonic() - sent
        replica = dict(future.initial_metadata() or ()).get("x-replica", "-")
        code = future.code()
        rec.add(sent - t0, latency, code == grpc.StatusCode.OK, code.name, replica, request_id)

    for i in range(int(rate * duration)):
        delay = t0 + i / rate - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        request_id = str(uuid.uuid4())
        sent = time.monotonic()
        future = stub.GetProduct.future(
            checkout_pb2.GetProductRequest(product_id=product_id),
            timeout=TIMEOUT,
            metadata=(("x-request-id", request_id),),
        )
        future.add_done_callback(lambda f, s=sent, r=request_id: done(f, s, r))
        pending.append(future)
    for future in pending:  # wait for stragglers
        try:
            future.result()
        except grpc.RpcError:
            pass
    time.sleep(0.2)
    channel.close()


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarise(name, rec, rate, duration):
    rows = sorted(rec.results)
    ok = [r for r in rows if r[2]]
    lat = [r[1] for r in rows]
    replicas = Counter(r[4] for r in ok)
    timeline = defaultdict(Counter)
    for offset, _, success, status, replica, _ in rows:
        timeline[int(offset)][replica if success else f"error:{status}"] += 1
    summary = {
        "interface": name,
        "target_rps": rate,
        "duration_s": duration,
        "actual_send_rps": round(len(rows) / max(rows[-1][0], 1e-9), 2) if rows else 0,
        "sent": len(rows),
        "successes": len(ok),
        "failures": len(rows) - len(ok),
        "error_rate_pct": round(100 * (len(rows) - len(ok)) / max(len(rows), 1), 2),
        "client_p50_ms": round(1000 * pct(lat, 0.50), 1) if lat else None,
        "client_p95_ms": round(1000 * pct(lat, 0.95), 1) if lat else None,
        "share_pct": {k: round(100 * v / max(len(ok), 1), 1) for k, v in sorted(replicas.items())},
        "errors_by_status": dict(Counter(r[3] for r in rows if not r[2])),
        "sample_request_id": {k: next(r[5] for r in ok if r[4] == k) for k in sorted(replicas)},
        "timeline": {str(k): dict(v) for k, v in sorted(timeline.items())},
    }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=20, help="requests per second, per interface")
    parser.add_argument("--duration", type=float, default=60, help="seconds")
    parser.add_argument("--interface", choices=["http", "grpc", "both"], default="both")
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    product_id = make_product()
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    recorders = {}
    threads = []
    for name, fn in (("http", run_http), ("grpc", run_grpc)):
        if args.interface in (name, "both"):
            recorders[name] = Recorder()
            threads.append(threading.Thread(target=fn, args=(product_id, args.rate, args.duration, recorders[name], t0)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ended = datetime.now(timezone.utc)

    out = {
        "label": args.label,
        "start_utc": started.isoformat(timespec="seconds"),
        "end_utc": ended.isoformat(timespec="seconds"),
        "results": [summarise(n, r, args.rate, args.duration) for n, r in recorders.items()],
    }
    Path("results").mkdir(exist_ok=True)
    Path(f"results/{args.label}.json").write_text(json.dumps(out, indent=2))
    print(f"{args.label}: {out['start_utc']} -> {out['end_utc']}")
    for s in out["results"]:
        print(
            f"  {s['interface']:4} target={s['target_rps']}/s actual={s['actual_send_rps']}/s sent={s['sent']} "
            f"ok={s['successes']} fail={s['failures']} ({s['error_rate_pct']}%) "
            f"p50={s['client_p50_ms']}ms p95={s['client_p95_ms']}ms shares={s['share_pct']} errors={s['errors_by_status']}"
        )


if __name__ == "__main__":
    main()
