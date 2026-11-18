"""Replica identity and per-replica fault injection (Assignment 3).

Docker Compose scaling names the containers ``homeshop-app-1``, ``-2`` and
``-3``. Each container resolves those names through Docker's DNS, finds the
one that matches its own IP, and maps the index to A, B or C, so every replica gets a stable, bounded identifier
without per-replica configuration.
"""
import os
import socket
import time

_LETTERS = {"1": "A", "2": "B", "3": "C", "4": "D"}


def _resolve_replica_id() -> str:
    """Find which homeshop-app-N name resolves to this container's own IP."""
    if os.getenv("REPLICA_ID"):
        return os.environ["REPLICA_ID"]
    prefix = os.getenv("REPLICA_NAME_PREFIX", "homeshop-app-")
    for _ in range(10):
        try:
            my_ip = socket.gethostbyname(socket.gethostname())
            for index, letter in _LETTERS.items():
                try:
                    if socket.gethostbyname(f"{prefix}{index}") == my_ip:
                        return letter
                except OSError:
                    continue
        except OSError:
            pass
        time.sleep(0.5)
    return socket.gethostname()


REPLICA_ID = _resolve_replica_id()
os.environ["REPLICA_ID"] = REPLICA_ID  # read by the JSON log formatter

# Seconds added to every application request on this replica (0 = normal).
# Set only through POST /debug/delay on one replica; health checks are not delayed.
INJECTED_DELAY = {"seconds": 0.0}
