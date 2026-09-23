# HomeShop

A minimal e-commerce backend for home-based / small businesses (e.g. someone
selling candles, baked goods, or crafts from home). Built for the
Observability assignment. Parts A–C are implemented; fault injection (Part E)
comes later.

## Architecture (so far)

- **api** — FastAPI service: browse products, manage cart, checkout, check order status. Exposes `/metrics`. Also serves a simple storefront UI at `/` (`app/static/index.html`).
- **worker** — polls the database for pending orders and "processes" them
  by calling a simulated payment gateway (configurable latency/failure rate). Exposes `/metrics` on port 8001.
- **db** — Postgres, shared by api and worker
- **node-exporter** — exposes host CPU/memory/disk/network stats
- **prometheus** — scrapes api, worker, and node-exporter every 5s
- **grafana** — pre-provisioned with a Prometheus datasource and a starter dashboard ("HomeShop Overview")
- **filebeat** — reads Docker's JSON log files, decodes HomeShop's JSON events, and sends them to Elasticsearch
- **elasticsearch** — indexes searchable API and worker logs in `homeshop-logs-*`
- **kibana** — explores and searches the stored logs

## Requirements

- Docker and Docker Compose

## Run it

```bash
docker compose up --build
```

The API will be available at http://localhost:8000. Interactive docs at
http://localhost:8000/docs.

## Try it — via the storefront UI

Open **http://localhost:8000** in your browser. It's a small multi-page site:

- **Home** (`/index.html`) — landing page with featured products
- **Shop** (`/shop.html`) — full product listing with search
- **Product detail** (`/product.html?id=...`) — description, quantity picker, add to cart
- **Cart** (`/cart.html`) — adjust quantities, remove items
- **Checkout** (`/checkout.html`) — review total, place order
- **Order status** (`/order.html?id=...`) — live-polling status, cancel button
- **My Orders** (`/orders.html`) — order history for the current demo user
- **Seller dashboard** (`/admin.html`) — add/restock products, revenue and order overview across all customers

There is intentionally no password/account flow in this small assignment.
Instead, a "Shopping as" field selects a constrained demo identity (for
example `alice` or `niha`), stored in browser localStorage and sent as an
`X-HomeShop-User` header. The API enforces that identity: a customer can only
access their own cart/orders, and a seller can only create or restock their
own products and view orders for their products. This is a demo ownership
boundary, not production authentication; a real deployment would verify a
session or token server-side.

Products are public marketplace listings, so Alice may browse a product sold
by Niha, but she cannot restock, edit, or view Niha's seller dashboard. A cart
is limited to one seller at a time, which makes each order belong to exactly
one seller and avoids ambiguous multi-seller fulfillment.

This is a set of static HTML pages (vanilla JS, no build step, shared
`css/style.css` and `js/common.js`) served directly by FastAPI's
`StaticFiles` mount at `/`, registered *after* all the API routes so it
never shadows them — it talks to the same API you can also drive with
curl below.

## Try it — via curl / API docs

1. Add a product as a seller (the header establishes ownership):
   ```bash
  curl -X POST http://localhost:8000/products \
    -H "X-HomeShop-User: niha" \
     -H "Content-Type: application/json" \
     -d '{"name": "Lavender Candle", "description": "Hand-poured soy candle", "price_cents": 1200, "stock": 20}'
   ```

2. List products:
   ```bash
   curl http://localhost:8000/products
   ```

3. Add to cart (replace PRODUCT_ID with the id returned above):
   ```bash
  curl -X POST http://localhost:8000/cart/alice/add \
    -H "Content-Type: application/json" \
    -H "X-HomeShop-User: alice" \
     -d '{"product_id": "PRODUCT_ID", "quantity": 2}'
   ```

4. Checkout:
   ```bash
  curl -X POST http://localhost:8000/checkout/alice \
    -H "X-HomeShop-User: alice"
   ```
   This returns an order with status `pending`. Within ~1 second the
   background worker will pick it up and move it to `processing`, then
   `completed` or `failed` (payments fail randomly ~3% of the time by default).

5. Check order status (replace ORDER_ID):
   ```bash
   curl http://localhost:8000/orders/ORDER_ID
   ```

6. Cancel an order (only works while pending/processing):
   ```bash
   curl -X POST http://localhost:8000/orders/ORDER_ID/cancel
   ```

## Configuration

The worker's simulated payment gateway is controlled via environment
variables (set in `docker-compose.yml`), so behavior can be changed
without touching code:

| Variable | Purpose | Default |
|---|---|---|
| `PAYMENT_BASE_LATENCY_MS` | normal payment processing time | 80 |
| `PAYMENT_JITTER_MS` | +/- random jitter on latency | 40 |
| `PAYMENT_FAILURE_RATE` | chance a payment is declined (0-1) | 0.03 |
| `PAYMENT_EXTRA_DELAY_MS` | extra fixed delay for fault injection | 0 |
| `PAYMENT_EXTRA_DELAY_EVERY_N` | apply extra delay every Nth call | unset |

These last two are unused for now — they're there for the Part E
anomaly experiment (injecting a repeatable slowdown).

## Part B: Metrics

Once `docker compose up --build` is running:

- **Prometheus UI**: http://localhost:9090 — check Status → Targets to confirm all 3 scrape jobs (`homeshop-api`, `homeshop-worker`, `node-exporter`) show as "UP".
- **Grafana**: http://localhost:3000 — log in with `admin` / `admin` (you'll be asked to set a new password, or you can skip). Open Dashboards → **HomeShop Overview**, which is provisioned automatically.
- **Raw metrics**: http://localhost:8000/metrics (API) and http://localhost:8001/metrics (worker) — useful for seeing exactly what Prometheus scrapes.

Generate some traffic so the dashboard has something to show:
```bash
for i in $(seq 1 20); do
  curl -s -X POST http://localhost:8000/cart/alice/add \
    -H "Content-Type: application/json" \
    -d '{"product_id": "PRODUCT_ID", "quantity": 1}' > /dev/null
  curl -s -X POST http://localhost:8000/checkout/alice > /dev/null
  sleep 0.5
done
```

### Metrics reference

| Metric | Type | Unit | Labels | Recorded in | Purpose |
|---|---|---|---|---|---|
| `http_requests_total` | Counter | requests | `method`, `path`, `status` | `app/main.py`, `track_http_metrics` middleware | Overall API traffic and error rate by endpoint |
| `http_request_duration_seconds` | Histogram | seconds | `method`, `path` | `app/main.py`, `track_http_metrics` middleware | API responsiveness per endpoint (p50/p95/p99 via `histogram_quantile`) |
| `orders_placed_total` | Counter | orders | — | `app/main.py`, `checkout()` | Business: how many orders come in |
| `orders_cancelled_total` | Counter | orders | — | `app/main.py`, `cancel_order()` | Business: cancellation volume |
| `checkout_rejected_total` | Counter | attempts | `reason` (`empty_cart`, `out_of_stock`) | `app/main.py`, `checkout()` | Business: why checkouts fail before payment |
| `inventory_stock` | Gauge | units | `product_name`, `seller_id` | `app/main.py`, `create_product()`, `restock_product()`, `checkout()` | Business: live stock level per seller product |
| `orders_queue_depth` | Gauge | orders | — | `worker/worker.py`, `run()` | App: how backed up order processing currently is |
| `payment_processing_seconds` | Histogram | seconds | — | `worker/worker.py`, `process_one_order()` (via `payment_gateway.charge`) | App: payment-gateway latency; this is the metric we degrade on purpose in Part E |
| `order_fulfillment_seconds` | Summary | seconds | — | `worker/worker.py`, `process_one_order()` | Business: average time from "order placed" to "done" (avg only — see note below) |
| `orders_completed_total` | Counter | orders | — | `worker/worker.py`, `process_one_order()` | Business: successfully fulfilled orders |
| `orders_failed_total` | Counter | orders | `reason` | `worker/worker.py`, `process_one_order()` | Business: failed payments |
| `revenue_cents_total` | Counter | cents | — | `worker/worker.py`, `process_one_order()` | **Explored ourselves**: running revenue total, not covered in the class examples |

Node Exporter adds the standard host metrics (`node_cpu_seconds_total`,
`node_memory_MemAvailable_bytes`, `node_filesystem_avail_bytes`,
`node_network_receive_bytes_total`, etc.) under `instance="homeshop-dev-host"`
— that label is set in `prometheus/prometheus.yml` so the machine is named
rather than showing a raw container hostname.

### Why Python Summary has no percentiles

`order_fulfillment_seconds` is a `Summary`, which in the Python client only
tracks a running `_sum` and `_count` (giving you the average via
`rate(sum)/rate(count)`), not configurable quantiles like some other
client libraries support. For anything needing p95/p99 we use a
**Histogram** instead — see `payment_processing_seconds`, queried with
`histogram_quantile()` in the dashboard.

### Grafana panels and their queries (see `grafana/dashboards/homeshop.json`)

- **Orders placed (rate, per minute)** — `rate(orders_placed_total[5m]) * 60`. Counters only go up, so we plot the *rate of increase* rather than the raw cumulative number.
- **Order outcomes** — three `rate(...)*60` queries overlaid (`orders_completed_total`, `orders_failed_total`, `orders_cancelled_total`) to compare volumes at a glance.
- **Orders currently in queue** — `orders_queue_depth`, plotted directly since it's already a Gauge (current value, not a rate).
- **Inventory stock by product** — `inventory_stock`, with seller and product labels, so identically named products from different sellers remain separate lines.
- **Payment processing latency (p50/p95/p99)** — `histogram_quantile(0.95, sum(rate(payment_processing_seconds_bucket[5m])) by (le))` (and similarly for 0.50/0.99). Shows how many milliseconds most, and the slowest, payments take.
- **HTTP latency p95 by route** — same `histogram_quantile` pattern, grouped by `path` so you can see which endpoint is slowest.
- **Average fulfillment time** — `rate(order_fulfillment_seconds_sum[5m]) / rate(order_fulfillment_seconds_count[5m])`, the manual way to get an average out of a Summary.
- **Revenue collected** — `revenue_cents_total / 100`, a simple stat panel showing dollars.
- **Host CPU / memory** — standard Node Exporter queries, scoped to `instance="homeshop-dev-host"`.

## Clean up

```bash
docker compose down -v
```

The `-v` also removes the Postgres data volume, so you get a totally
fresh database next time.

## Part C: Centralized JSON logging

Starting `docker compose up --build` also starts the log pipeline. It can take
about a minute for Elasticsearch and Kibana to become ready.

- **Kibana:** http://localhost:5601. In Discover, create a data view named
  `homeshop-logs-*` and select `timestamp` as its time field.
- **Elasticsearch:** http://localhost:9200. This is exposed for local
  inspection only; security is deliberately disabled for this classroom,
  single-machine setup and it must not be exposed publicly.

### What is logged

The API and worker write one JSON object per event to stdout through
`app/observability_logging.py`. Every object contains `timestamp` (UTC),
`service`, `severity`, and `message`. API requests also receive a UUID
`request_id`, returned in `X-Request-ID`; callers may supply their own valid
UUID in `X-Request-ID` to trace a request. Business events contain safe identifiers
such as `order_id` and operational fields such as `status_code`, `duration_ms`,
or `total_cents`. We intentionally do not log request bodies, headers,
payment data, or user IDs.

Examples of useful events are `http_request_complete`, `checkout_rejected`,
`order_placed`, `order_processing_started`, `order_completed`, and
`order_failed`. This gives the API request path and the worker's order outcome
without creating Prometheus labels from request or order IDs.

### Pipeline, storage, and retention

Docker's default `json-file` driver records each stdout line. Filebeat reads
`/var/lib/docker/containers/*/*.log`, unwraps Docker's envelope, and decodes
the JSON in `message`; the fields above become native Elasticsearch fields
rather than text that needs a grok parser. `add_docker_metadata` also adds
container metadata. Filebeat discards non-HomeShop container logs so the index
contains only `homeshop-api` and `homeshop-worker` events.

Elasticsearch stores daily indexes named `homeshop-logs-YYYY.MM.DD` in the
named `homeshop_elasticsearch_data` volume. They survive ordinary container
restarts and `docker compose down`; they are deleted only by an explicit index
deletion or by `docker compose down -v` (the latter removes *all* project data,
including orders and metrics). For this small local assignment there is no
automatic retention policy. To delete a specific old log day while retaining
everything else, use a confirmed date, for example:

```bash
curl -X DELETE 'http://localhost:9200/homeshop-logs-2026.09.09'
```

### Searching an event in Kibana

1. Place an order or make any API request, then copy the response's
   `X-Request-ID` header.
2. In Discover, choose a time range that includes the request and search
   `request_id: "YOUR-REQUEST-ID"`. This finds the corresponding API event.
3. To find failures across the worker, use
   `service: "homeshop-worker" and severity: "WARNING"`; to follow one order,
   search `order_id: "YOUR-ORDER-ID"`.

One original application line looks like this (values vary):

```json
{"timestamp":"2026-09-23T08:30:00.000Z","service":"homeshop-api","severity":"INFO","message":"http_request_complete","request_id":"7c9d...","method":"POST","path":"/checkout/{user_id}","status_code":200,"duration_ms":18.42}
```

After Filebeat decoding, Kibana shows `timestamp`, `service`, `severity`,
`message`, `request_id`, `method`, `path`, `status_code`, and `duration_ms` as
separate searchable fields.

## Status

- [x] Part A — working app
- [x] Part B — Prometheus + Grafana metrics (this step)
- [x] Part C — Filebeat + Elasticsearch + Kibana logging
- [ ] Part D — architecture diagram + trace-a-metric/log walkthrough
- [ ] Part E — anomaly experiment + cardinality experiment
