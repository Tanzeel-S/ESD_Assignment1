"""Integration demonstration for successful and failed checkout over HTTP and gRPC.

Run inside the Compose network using:
    docker compose exec grpc python scripts/test_part_b.py
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import grpc

API_URL = os.getenv("API_URL", "http://api:8000")
GRPC_TARGET = os.getenv("GRPC_TARGET", "grpc:50051")


def http_call(method, path, *, user_id, payload=None, request_id=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"X-HomeShop-User": user_id}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if request_id:
        headers["X-Request-ID"] = request_id
    request = urllib.request.Request(
        API_URL + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def prepare_cart(user_id):
    status, product = http_call(
        "POST",
        "/products",
        user_id="niha",
        payload={
            "name": f"Part B Demo {uuid.uuid4().hex[:8]}",
            "description": "Integration test item",
            "price_cents": 1234,
            "stock": 10,
        },
    )
    assert status == 200, f"Product setup failed: {status} {product}"
    status, result = http_call(
        "POST",
        f"/cart/{user_id}/add",
        user_id=user_id,
        payload={"product_id": product["id"], "quantity": 1},
    )
    assert status == 200, f"Cart setup failed: {status} {result}"


def main():
    ready_channel = grpc.insecure_channel(GRPC_TARGET)
    try:
        grpc.channel_ready_future(ready_channel).result(timeout=20)
    finally:
        ready_channel.close()

    suffix = uuid.uuid4().hex[:8]
    http_user = f"http{suffix}"
    grpc_user = f"rpc{suffix}"
    http_fail_user = f"httpfail{suffix}"
    grpc_fail_user = f"rpcfail{suffix}"

    request_id = str(uuid.uuid4())
    prepare_cart(http_user)
    status, result = http_call(
        "POST", f"/checkout/{http_user}", user_id=http_user, request_id=request_id
    )
    print(f"HTTP success: status={status} order_id={result.get('id')} request_id={request_id}")
    assert status == 200

    status, result = http_call("POST", f"/checkout/{http_fail_user}", user_id=http_fail_user)
    print(f"HTTP failure: status={status} body={result}")
    assert status == 400

    prepare_cart(grpc_user)
    grpc_request_id = str(uuid.uuid4())
    success = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_service.client",
            grpc_user,
            "--deadline",
            "3",
            "--request-id",
            grpc_request_id,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    print(success.stdout.strip())
    assert success.returncode == 0, success.stderr

    failure = subprocess.run(
        [sys.executable, "-m", "grpc_service.client", grpc_fail_user, "--deadline", "3"],
        check=False,
        capture_output=True,
        text=True,
    )
    print(failure.stdout.strip())
    assert failure.returncode == 1
    assert "FAILED_PRECONDITION" in failure.stdout


if __name__ == "__main__":
    main()
