"""
Prometheus metrics for the HomeShop API.

Naming follows Prometheus convention: unit suffix on the metric name
(_seconds, _total, _cents), and labels kept to low-cardinality values
only (HTTP method/route template, product name, rejection reason) —
NEVER raw IDs. That rule is deliberately violated on purpose later, in
a throwaway demo metric, for the Part E.2 cardinality experiment.
"""
from prometheus_client import Counter, Gauge, Histogram

# ---- App metrics (technical: request handling) ----

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the API",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "HTTP application requests currently being handled (excludes /metrics and /healthz)",
)

REPLICA_INFO = Gauge(
    "homeshop_replica_info",
    "Always 1; the replica label identifies which replica (A, B, C) exported these metrics",
    ["replica"],
)

# ---- Business metrics ----

ORDERS_PLACED_TOTAL = Counter(
    "orders_placed_total",
    "Total number of orders placed (checkout succeeded, before payment)",
)

ORDERS_CANCELLED_TOTAL = Counter(
    "orders_cancelled_total",
    "Total number of orders cancelled by a customer/admin",
)

CHECKOUT_REJECTED_TOTAL = Counter(
    "checkout_rejected_total",
    "Checkout attempts rejected before an order was created",
    ["reason"],  # e.g. "empty_cart", "out_of_stock"
)

INVENTORY_STOCK = Gauge(
    "inventory_stock",
    "Current stock level for a product",
    # Seller IDs are small, stable demo identities. Keeping this dimension
    # prevents two sellers' similarly named products overwriting one series.
    ["product_name", "seller_id"],
)

# ---- Experiment / Demo metrics ----

CARDINALITY_TEST_TOTAL = Counter(
    "cardinality_test_total",
    "High-cardinality test counter with unique request_id labels for Part E.2 experiment",
    ["request_id"],
)

