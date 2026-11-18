#!/usr/bin/env python3
"""Part A checks for the checkout task: valid, invalid, repeated, rate limit.

Run:  python3 scripts/test_part_a.py   (or inside the grpc container with API_URL set)
"""

import json
import os
import urllib.error
import urllib.request
import uuid

API_BASE = os.getenv("API_URL", "http://localhost:8000")


def http(method, path, user, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", "X-HomeShop-User": user, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read() or b"null")


def prepare_cart(user):
    _, _, product = http("POST", "/products", "niha", {"name": f"Part A {uuid.uuid4().hex[:6]}", "price_cents": 750, "stock": 5})
    status, _, body = http("POST", f"/cart/{user}/add", user, {"product_id": product["id"], "quantity": 1})
    assert status == 200, body


def check(label, condition, detail):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}: {detail}")
    assert condition


def main():
    run = uuid.uuid4().hex[:6]

    # 1. Valid request
    user, key = f"pa{run}", str(uuid.uuid4())
    prepare_cart(user)
    status, _, order = http("POST", f"/checkout/{user}", user, headers={"Idempotency-Key": key})
    check("valid checkout", status == 200, f"HTTP {status} order_id={order.get('id')} total_cents={order.get('total_cents')}")

    # 2. Repeated request with the same Idempotency-Key
    status, _, repeat = http("POST", f"/checkout/{user}", user, headers={"Idempotency-Key": key})
    check("repeated checkout", status == 200 and repeat["id"] == order["id"],
          f"HTTP {status} order_id={repeat.get('id')} (same order, cart not charged twice)")
    status, _, orders = http("GET", "/orders", user)
    check("one order only", len(orders) == 1, f"orders for {user}: {len(orders)}")

    # 3. Invalid input
    status, _, body = http("POST", f"/checkout/{user}", "Bad User!")
    check("invalid identity", status == 400 and body["error"]["code"] == "http_error", f"HTTP {status} {body}")
    status, _, body = http("POST", f"/cart/{user}/add", user, {"product_id": "x", "quantity": 0})
    check("invalid quantity", status == 422 and body["error"]["code"] == "validation_error", f"HTTP {status} {body['error']['message']}")
    status, _, body = http("POST", f"/checkout/{user}", user)
    check("empty cart", status == 400 and body["error"]["code"] == "empty_cart", f"HTTP {status} {body}")
    status, _, body = http("GET", "/no-such-route", user)
    check("unknown route", status == 404 and "error" in body, f"HTTP {status} {body}")

    # 4. Rate limit: 10 checkout calls / 60 s per user; the 11th gets 429
    user = f"rl{run}"
    statuses = [http("POST", f"/checkout/{user}", user)[0] for _ in range(10)]
    check("accepted by limiter", 429 not in statuses, f"10 calls -> {statuses} (400 = empty cart, i.e. reached the handler)")
    status, headers, body = http("POST", f"/checkout/{user}", user)
    check("rate limited", status == 429 and "Retry-After" in headers,
          f"HTTP {status} Retry-After={headers.get('Retry-After')} body={body}")

    # 5. Use the OpenAPI spec to test the endpoint: the live 429 must match the documented contract
    _, _, spec = http("GET", "/openapi.json", user)
    documented = spec["paths"]["/checkout/{user_id}"]["post"]["responses"]["429"]
    required = spec["components"]["schemas"]["ErrorBody"]["required"]
    check("429 matches OpenAPI", "Retry-After" in documented["headers"] and all(k in body["error"] for k in required),
          f"spec documents 429 + Retry-After; body has required fields {required}")


if __name__ == "__main__":
    main()
