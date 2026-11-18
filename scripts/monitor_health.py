#!/usr/bin/env python3
"""Poll Envoy's admin /clusters every 0.25 s and log health changes of one replica.

    python3 scripts/monitor_health.py homeshop-app-2 120 > results/failure-health.log

Each line: unix_time  cluster  state  (state = healthy / unhealthy flags / absent).
"""
import sys
import time
import urllib.request

host, seconds = sys.argv[1], float(sys.argv[2])
last = {}
end = time.time() + seconds
while time.time() < end:
    try:
        text = urllib.request.urlopen("http://localhost:9901/clusters", timeout=1).read().decode()
    except OSError as err:
        text = ""
        print(f"{time.time():.2f} admin-unreachable {type(err).__name__}", flush=True)
    ips, flags = {}, {}
    for line in text.splitlines():
        parts = line.split("::")
        if len(parts) == 4 and parts[0] in ("app_http", "app_grpc"):
            cluster, addr, key, value = parts
            if key == "hostname":
                ips[(cluster, addr)] = value
            elif key == "health_flags":
                flags[(cluster, addr)] = value
    for cluster in ("app_http", "app_grpc"):
        states = [flags.get(k, "?") + "@" + k[1] for k, h in ips.items() if h == host and k[0] == cluster]
        state = ",".join(sorted(states)) or "absent"
        if last.get(cluster) != state:
            print(f"{time.time():.2f} {cluster} {state}", flush=True)
            last[cluster] = state
    time.sleep(0.25)
