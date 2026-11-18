"""Generate repeatable HTTP and gRPC checkout successes and failures.

Run from the Docker Compose gRPC service:
    docker compose exec grpc python scripts/run_grpc_metrics_demo.py --count 4
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grpc

from grpc_service import checkout_pb2, checkout_pb2_grpc

API_URL = os.getenv("API_URL", "http://api:8000")
GRPC_TARGET = os.getenv("GRPC_TARGET", "grpc:50051")


def http_json(method, path, *, user_id, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"X-HomeShop-User": user_id}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{API_URL}{path}", data=body, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def prepare_cart(user_id, label):
    status, product = http_json(
        "POST",
        "/products",
        user_id="niha",
        payload={
            "name": f"Metrics {label} {uuid.uuid4().hex[:6]}",
            "description": "Repeatable gRPC metrics sample",
            "price_cents": 100,
            "stock": 10,
        },
    )
    if status != 200:
        raise RuntimeError(f"Product setup failed: HTTP {status}: {product}")
    status, result = http_json(
        "POST",
        f"/cart/{user_id}/add",
        user_id=user_id,
        payload={"product_id": product["id"], "quantity": 1},
    )
    if status != 200:
        raise RuntimeError(f"Cart setup failed: HTTP {status}: {result}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=4, help="success/failure samples per protocol")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.25,
        help="seconds between samples; span the Prometheus scrape interval to populate rates/quantiles",
    )
    args = parser.parse_args()
    if not 1 <= args.count <= 20:
        parser.error("--count must be between 1 and 20")
    if not 0 <= args.interval <= 30:
        parser.error("--interval must be between 0 and 30 seconds")

    channel = grpc.insecure_channel(GRPC_TARGET)
    try:
        grpc.channel_ready_future(channel).result(timeout=20)
    finally:
        channel.close()

    token = uuid.uuid4().hex[:8]
    http_ok = http_errors = grpc_ok = grpc_errors = 0

    for index in range(args.count):
        http_user = f"hm{token}{index}"
        prepare_cart(http_user, f"http{index}")
        status, result = http_json(
            "POST", f"/checkout/{http_user}", user_id=http_user
        )
        http_ok += int(status == 200)
        print(f"HTTP checkout success sample {index + 1}: status={status}")
        if status != 200:
            print(f"  response={result}")

        empty_http_user = f"hf{token}{index}"
        status, result = http_json(
            "POST", f"/checkout/{empty_http_user}", user_id=empty_http_user
        )
        http_errors += int(status >= 400)
        print(f"HTTP empty-cart sample {index + 1}: status={status}")

        grpc_user = f"gm{token}{index}"
        prepare_cart(grpc_user, f"grpc{index}")
        request_id = str(uuid.uuid4())
        with grpc.insecure_channel(GRPC_TARGET) as channel:
            stub = checkout_pb2_grpc.CheckoutServiceStub(channel)
            try:
                response = stub.Checkout(
                    checkout_pb2.CheckoutRequest(user_id=grpc_user),
                    timeout=3.0,
                    metadata=(("x-request-id", request_id),),
                )
                grpc_ok += 1
                print(f"gRPC checkout success sample {index + 1}: code=OK order_id={response.order_id}")
            except grpc.RpcError as error:
                print(f"gRPC success sample unexpectedly failed: {error.code().name}: {error.details()}")

            empty_grpc_user = f"gf{token}{index}"
            try:
                stub.Checkout(
                    checkout_pb2.CheckoutRequest(user_id=empty_grpc_user),
                    timeout=3.0,
                    metadata=(("x-request-id", str(uuid.uuid4())),),
                )
                print("gRPC empty-cart sample unexpectedly succeeded")
            except grpc.RpcError as error:
                grpc_errors += 1
                print(
                    f"gRPC empty-cart sample {index + 1}: "
                    f"code={error.code().name} details={error.details()}"
                )

        if index + 1 < args.count and args.interval:
            time.sleep(args.interval)

    print(
        "Totals: "
        f"HTTP success={http_ok}, HTTP errors={http_errors}, "
        f"gRPC success={grpc_ok}, gRPC errors={grpc_errors}"
    )


if __name__ == "__main__":
    main()
