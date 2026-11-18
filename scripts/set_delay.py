#!/usr/bin/env python3
"""Set a fixed delay on ONE replica, bypassing Envoy (A3 Part D).

    docker compose run --rm tools python scripts/set_delay.py B 0.2   # B adds 200 ms
    docker compose run --rm tools python scripts/set_delay.py B 0     # restore
"""
import json
import sys
import urllib.request

HOSTS = {"A": "homeshop-app-1", "B": "homeshop-app-2", "C": "homeshop-app-3"}
replica, seconds = sys.argv[1].upper(), float(sys.argv[2])
req = urllib.request.Request(
    f"http://{HOSTS[replica]}:8000/debug/delay",
    data=json.dumps({"seconds": seconds}).encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=5) as resp:
    print(resp.read().decode())
