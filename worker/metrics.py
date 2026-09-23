"""
Prometheus metrics for the HomeShop worker.

The worker runs as its own process/container, so it has its own metrics
registry and its own /metrics HTTP server (default registry + a small
http server started via start_metrics_server()).
"""
from prometheus_client import Counter, Gauge, Histogram, Summary, start_http_server

ORDERS_COMPLETED_TOTAL = Counter(
    "orders_completed_total",
    "Total number of orders successfully paid and fulfilled",
)

ORDERS_FAILED_TOTAL = Counter(
    "orders_failed_total",
    "Total number of orders that failed (payment declined)",
    ["reason"],
)

REVENUE_CENTS_TOTAL = Counter(
    "revenue_cents_total",
    "Total revenue collected, in cents, from completed orders. "
    "This is the one metric we explored ourselves beyond the class "
    "examples: it turns individual order events into a running "
    "business total that a shop owner would actually care about.",
)

ORDERS_QUEUE_DEPTH = Gauge(
    "orders_queue_depth",
    "Number of orders currently pending or processing (not yet completed/failed)",
)

PAYMENT_PROCESSING_SECONDS = Histogram(
    "payment_processing_seconds",
    "Time spent in the simulated payment gateway per order",
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3),
)

ORDER_FULFILLMENT_SECONDS = Summary(
    "order_fulfillment_seconds",
    "End-to-end time from order placement to completion/failure. "
    "Python Summaries only give us count/sum (=> average) with no "
    "percentiles, per the assignment note; use the Histogram above "
    "(payment_processing_seconds) for p95/p99 instead.",
)


def start_metrics_server(port: int = 8001):
    start_http_server(port)
