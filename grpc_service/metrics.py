"""Prometheus metrics for the HomeShop gRPC server process."""

from prometheus_client import Counter, Gauge, Histogram

# Assignment 3: the gRPC server runs inside each replica's API process, so these
# metrics are exported on that replica's HTTP /metrics endpoint.

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
