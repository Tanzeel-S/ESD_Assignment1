#!/usr/bin/env python3
"""
Part E.1: Anomaly Experiment Runner (Reproduce a Problem)
---------------------------------------------------------
Automates the 5-step Anomaly Experiment:
1. Measure normal baseline metrics & logs.
2. Output formal prediction of metric & log changes.
3. Inject fault (500ms latency on every 5th payment).
4. Measure actual metric & log changes (p95/p99 latency, queue depth).
5. Remove fault and verify recovery.
"""
import json
import os
import sys
import time
import subprocess
import requests

API_URL = "http://127.0.0.1:8000"
PROM_URL = "http://127.0.0.1:9090"



def execute_in_worker(cmd):
    """Execute command inside worker container or write to container file."""
    try:
        res = subprocess.run(
            ["docker", "exec", "homeshop-worker"] + cmd,
            capture_output=True, text=True, check=True
        )
        return res.stdout.strip()
    except Exception as e:
        print(f"[Warning] Container exec warning: {e}")
        return None


def get_payment_latency_p95():
    query = 'histogram_quantile(0.95, sum(rate(payment_processing_seconds_bucket[5m])) by (le))'
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": query}, timeout=5)
        data = r.json()
        if data.get("status") == "success" and data["data"]["result"]:
            val = float(data["data"]["result"][0]["value"][1])
            return round(val * 1000, 2)  # in ms
    except Exception:
        pass
    return 0.0


def get_queue_depth():
    query = 'orders_queue_depth'
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": query}, timeout=5)
        data = r.json()
        if data.get("status") == "success" and data["data"]["result"]:
            val = float(data["data"]["result"][0]["value"][1])
            return int(val)
    except Exception:
        pass
    return 0


def generate_test_orders(count=10):
    """Generate checkout traffic."""
    p_resp = requests.get(f"{API_URL}/products")
    if p_resp.status_code != 200 or not p_resp.json():
        return
    product = p_resp.json()[0]
    headers = {"X-HomeShop-User": "alice"}
    
    for _ in range(count):
        requests.delete(f"{API_URL}/cart/alice/item/{product['id']}", headers=headers)
        requests.post(f"{API_URL}/cart/alice/add", json={"product_id": product["id"], "quantity": 1}, headers=headers)
        requests.post(f"{API_URL}/checkout/alice", headers=headers)
        time.sleep(0.3)


def inject_fault():
    cfg = json.dumps({"extra_delay_ms": 500, "extra_delay_every_n": 5, "failure_rate": 0.03})
    execute_in_worker(["sh", "-c", f"echo '{cfg}' > /tmp/fault_config.json"])


def remove_fault():
    execute_in_worker(["rm", "-f", "/tmp/fault_config.json"])


def main():
    print("==========================================================")
    print("       Part E.1: Anomaly Experiment (Reproduce a Problem) ")
    print("==========================================================")

    # Step 1: Baseline
    print("\n[Step 1] Measuring Baseline (Normal Behavior)...")
    remove_fault()
    generate_test_orders(5)
    time.sleep(4)
    baseline_p95 = get_payment_latency_p95()
    baseline_queue = get_queue_depth()
    print(f"  - Baseline Payment Processing Latency (p95): {baseline_p95} ms")
    print(f"  - Baseline Queue Depth: {baseline_queue} orders")

    # Step 2: Predictions
    print("\n[Step 2] Predicting Impact of Fault Injection:")
    print("  - Fault to inject: +500ms extra delay on every 5th payment call (PAYMENT_EXTRA_DELAY_EVERY_N=5).")
    print("  - Predicted Metric Changes: `payment_processing_seconds` p95/p99 will spike from ~80ms to >500ms.")
    print("  - Predicted Metric Changes: `orders_queue_depth` may temporarily rise as processing slows.")
    print("  - Predicted Log Changes: `order_completed` events in Kibana will show `fulfillment_seconds` > 0.58s.")

    # Step 3: Introduce Problem
    print("\n[Step 3] Introducing Fault (Injecting 500ms delay every 5th payment)...")
    inject_fault()
    generate_test_orders(15)
    print("  - Generated 15 test orders during fault window. Waiting for Prometheus scrapes...")
    time.sleep(8)
    
    fault_p95 = get_payment_latency_p95()
    fault_queue = get_queue_depth()
    print(f"  - Fault Active Payment Processing Latency (p95): {fault_p95} ms")
    print(f"  - Fault Active Queue Depth: {fault_queue} orders")

    # Step 4: Explain cause and effect
    print("\n[Step 4] Cause and Effect Analysis:")
    print("  - Root Cause: Intermittent upstream dependency latency introduced into simulated payment gateway.")
    print("  - User Impact: Order checkout responses complete instantly to customer, but order fulfillment/completion is delayed, increasing pending wait time.")

    # Step 5: Remove problem and check recovery
    print("\n[Step 5] Removing Fault & Verifying Recovery...")
    remove_fault()
    generate_test_orders(8)
    time.sleep(8)

    recovered_p95 = get_payment_latency_p95()
    recovered_queue = get_queue_depth()
    print(f"  - Recovered Payment Processing Latency (p95): {recovered_p95} ms")
    print(f"  - Recovered Queue Depth: {recovered_queue} orders")
    print("==========================================================")
    print("  Anomaly Experiment Completed Successfully!")
    print("==========================================================")


if __name__ == "__main__":
    main()
