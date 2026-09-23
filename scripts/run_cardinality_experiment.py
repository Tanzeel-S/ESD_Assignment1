#!/usr/bin/env python3
"""
Part E.2: Cardinality Explosion Experiment Runner
-------------------------------------------------
Automates the cardinality explosion experiment:
1. Queries baseline count of `cardinality_test_total` time series in Prometheus.
2. Triggers 100 unique request ID label values via POST /experiment/cardinality/run.
3. Waits for Prometheus scrape interval.
4. Queries Prometheus again with `count(cardinality_test_total)` to demonstrate 100 series explosion.
"""
import sys
import time
import requests

API_URL = "http://127.0.0.1:8000"
PROM_URL = "http://127.0.0.1:9090"



def query_prometheus_count():
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query", params={"query": "count(cardinality_test_total)"}, timeout=5)
        data = r.json()
        if data.get("status") == "success" and data["data"]["result"]:
            val = data["data"]["result"][0]["value"][1]
            return int(val)
    except Exception as e:
        print(f"[Warning] Could not query Prometheus: {e}")
    return 0


def main():
    print("==========================================================")
    print("       Part E.2: Cardinality Explosion Experiment         ")
    print("==========================================================")

    initial_series = query_prometheus_count()
    print(f"[Stage 1] Initial Prometheus series count for `cardinality_test_total`: {initial_series}")

    print("\n[Stage 2] Injecting 100 unique request_id labels via API...")
    try:
        r = requests.post(f"{API_URL}/experiment/cardinality/run?count=100", timeout=10)
        res = r.json()
        print(f"API Response: {res['message']}")
        print(f"Sample IDs: {res['sample_ids']}")
    except Exception as e:
        print(f"[Error] API request failed: {e}")
        sys.exit(1)

    print("\n[Stage 3] Waiting 7 seconds for Prometheus scrape cycle...")
    time.sleep(7)

    new_series = query_prometheus_count()
    print(f"[Stage 4] Post-experiment Prometheus series count for `cardinality_test_total`: {new_series}")
    print(f"Series Growth: {new_series - initial_series} new time series added!")

    print("\n[Stage 5] Explanation & Key Findings:")
    print("  - Each unique label combination (metric_name + labels) creates a separate time-series stream in Prometheus TSDB.")
    print("  - 100 unique request IDs created 100 distinct time series in memory.")
    print("  - At production scale (millions of requests), putting raw IDs in labels causes severe RAM bloat, slow queries, and Prometheus crashes.")
    print("  - Conclusion: High-cardinality values (UUIDs, user IDs, request IDs) MUST belong in LOGS (Elasticsearch/Kibana), NOT METRICS labels.")
    print("==========================================================")


if __name__ == "__main__":
    main()
