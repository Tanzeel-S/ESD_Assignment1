"""Prometheus metrics for the HomeShop gRPC server process."""

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server

from app.metrics import INVENTORY_STOCK

# Shared checkout code also updates the inventory Gauge, but a second copy from
# this process would go stale and duplicate the API's series. Counters (orders
# placed / rejected) are kept and summed across jobs in Grafana instead.
REGISTRY.unregister(INVENTORY_STOCK)

GRPC_RPC_CALLS_TOTAL = Counter(
    "homeshop_grpc_rpc_calls_total",
    "Completed gRPC server calls, partitioned by bounded RPC method and gRPC status code.",
    ("rpc_method", "status"),
)

GRPC_RPC_DURATION_SECONDS = Histogram(
    "homeshop_grpc_rpc_duration_seconds",
    "Time spent handling a gRPC call on the server, in seconds.",
    ("rpc_method",),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

GRPC_RPC_IN_FLIGHT = Gauge(
    "homeshop_grpc_rpc_in_flight",
    "Number of gRPC calls currently being handled by the server.",
    ("rpc_method",),
)


def start_metrics_server(port: int = 8002) -> None:
    """Expose this process's Prometheus registry on a separate HTTP port."""
    start_http_server(port)
