#!/usr/bin/env python3
"""Part A reliability caller for HomeShop's POST /checkout/{user_id}.

Acts like another service calling the checkout API. It uses only the Python
standard library and demonstrates, in one run:
- a per-request timeout,
- limited retry with exponential backoff (only for 5xx / timeouts, and only
  because every checkout carries an Idempotency-Key, so a repeat cannot
  create a second order),
- a circuit breaker that opens after repeated failures, blocks calls, and
  closes again after a successful trial call once the recovery period passes.

Failures are injected with the demo-only POST /debug/checkout-fault endpoint.

Run:  python3 scripts/part_a_http_caller.py            (API on localhost:8000)
  or: docker compose exec grpc python scripts/part_a_http_caller.py
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid

API_BASE = os.getenv("API_URL", "http://localhost:8000")
TIMEOUT_SECONDS = 1.5          # per HTTP attempt
RETRY_ATTEMPTS = 3             # total attempts per logical call
BACKOFF_SECONDS = 0.5          # 0.5 s, then 1.0 s
RETRYABLE = {502, 503, 504, 599}   # 599 = timeout / connection error (client-side)
BREAKER_FAILURE_THRESHOLD = 2  # consecutive failed logical calls before opening
BREAKER_RECOVERY_SECONDS = 5   # time OPEN before a half-open trial call


def http(method, path, user, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API_BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "X-HomeShop-User": user, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return 599, {"error": {"code": "timeout_or_transport", "message": str(exc)}}


class CircuitBreaker:
    def __init__(self, threshold, recovery_seconds):
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self.failures = 0
        self.state = "CLOSED"
        self.opened_at = 0.0

    def allow_request(self):
        if self.state == "OPEN":
            waited = time.monotonic() - self.opened_at
            if waited < self.recovery_seconds:
                print(f"  breaker OPEN: call blocked ({self.recovery_seconds - waited:.1f}s until trial)")
                return False
            self.state = "HALF_OPEN"
            print("  breaker HALF_OPEN: allowing one trial call")
        return True

    def record_success(self):
        if self.state != "CLOSED":
            print("  breaker CLOSED: trial call succeeded")
        self.failures = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failures += 1
        if self.state == "HALF_OPEN" or self.failures >= self.threshold:
            self.state = "OPEN"
            self.opened_at = time.monotonic()
            print(f"  breaker OPEN after {self.failures} consecutive failed call(s)")
        else:
            print(f"  breaker CLOSED: failure {self.failures}/{self.threshold}")


def checkout(breaker, user, idem_key):
    """One logical checkout: breaker check, then up to RETRY_ATTEMPTS with backoff."""
    if not breaker.allow_request():
        return None, None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        started = time.monotonic()
        status, body = http("POST", f"/checkout/{user}", user, headers={"Idempotency-Key": idem_key})
        print(f"  attempt {attempt}: HTTP {status} in {time.monotonic() - started:.2f}s")
        if status < 300:
            breaker.record_success()
            return status, body
        if status not in RETRYABLE or attempt == RETRY_ATTEMPTS:
            break
        delay = BACKOFF_SECONDS * 2 ** (attempt - 1)
        print(f"  retrying in {delay:.1f}s (same Idempotency-Key)")
        time.sleep(delay)
    breaker.record_failure()
    return status, body


def prepare_cart(user):
    status, product = http("POST", "/products", "niha", {"name": f"Caller demo {uuid.uuid4().hex[:6]}", "price_cents": 500, "stock": 5})
    assert status == 200, product
    status, body = http("POST", f"/cart/{user}/add", user, {"product_id": product["id"], "quantity": 1})
    assert status == 200, body


def set_fault(mode, count=1):
    http("POST", "/debug/checkout-fault", "niha", {"mode": mode, "count": count})


def main():
    run = uuid.uuid4().hex[:6]
    print(f"API={API_BASE} timeout={TIMEOUT_SECONDS}s attempts={RETRY_ATTEMPTS} "
          f"backoff={BACKOFF_SECONDS}s x2 breaker_threshold={BREAKER_FAILURE_THRESHOLD} "
          f"breaker_recovery={BREAKER_RECOVERY_SECONDS}s")
    breaker = CircuitBreaker(BREAKER_FAILURE_THRESHOLD, BREAKER_RECOVERY_SECONDS)
    set_fault("off")

    print("\n1) Success")
    user, key = f"ok{run}", str(uuid.uuid4())
    prepare_cart(user)
    _, first = checkout(breaker, user, key)
    print(f"  order_id={first['id']}")

    print("\n2) Repeat with the same Idempotency-Key (no duplicate order)")
    _, again = checkout(breaker, user, key)
    print(f"  order_id={again['id']} same_order={again['id'] == first['id']}")

    print("\n3) Transient 503, retried with backoff")
    user = f"retry{run}"
    prepare_cart(user)
    set_fault("error", 1)
    status, body = checkout(breaker, user, str(uuid.uuid4()))
    print(f"  final: HTTP {status} order_id={body.get('id')}")

    print("\n4) Timeout (server takes 3s > 1.5s), retried")
    user = f"slow{run}"
    prepare_cart(user)
    set_fault("slow", 1)
    status, body = checkout(breaker, user, str(uuid.uuid4()))
    print(f"  final: HTTP {status} order_id={body.get('id')}")

    print("\n5) Server keeps failing -> breaker opens, blocks, then recovers")
    user = f"cb{run}"
    prepare_cart(user)
    set_fault("error", 100)
    key = str(uuid.uuid4())
    for call in range(1, 5):
        print(f" call {call}")
        status, _ = checkout(breaker, user, key)
        if status is None:
            print("  -> not sent to the server")
    set_fault("off")
    print(f" server healthy again; waiting {BREAKER_RECOVERY_SECONDS}s recovery period")
    time.sleep(BREAKER_RECOVERY_SECONDS)
    print(" call 5")
    status, body = checkout(breaker, user, key)
    print(f"  final: HTTP {status} order_id={body.get('id')} breaker={breaker.state}")


if __name__ == "__main__":
    main()
