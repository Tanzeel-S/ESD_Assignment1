# HomeShop

A minimal e-commerce backend for home-based / small businesses (e.g. someone
selling candles, baked goods, or crafts from home). Built for the
Observability assignment (A1) extended with HTTP API improvements, gRPC and metrics (A2), then load-balanced behind Envoy (A3). Reports: `REPORT.md` (A1), `ASSIGNMENT2_REPORT.md` (A2), `ASSIGNMENT3_REPORT.md` (A3).

## Architecture

- **envoy** — the only entry point. HTTP on `localhost:8000`, gRPC on `localhost:50051`, admin and metrics on `localhost:9901`. It balances requests across the replicas; the policy file is chosen by `ENVOY_CONFIG`.
- **app** (×3: `homeshop-app-1/2/3` = replicas **A/B/C**) — the FastAPI HTTP API (storefront UI, `/metrics`, `/healthz`) and the gRPC service (`Checkout`, `GetProduct`, gRPC health) in one process per replica. The replicas are not published to the host.
- **worker** — polls the database for pending orders and "processes" them
  by calling a simulated payment gateway (configurable latency/failure rate). Exposes `/metrics` on port 8001.
- **db** — Postgres, shared by all replicas and the worker. It also holds the shared idempotency records and rate-limit counters.
- **tools** (profile `tools`) — a client container for tests, the load generator and the A2 scripts: `docker compose run --rm tools python scripts/...`
- **node-exporter** — exposes host CPU/memory/disk/network stats
- **prometheus** — scrapes each replica directly (labelled `replica=A/B/C`), Envoy, the worker and node-exporter every 5 s
- **grafana** — dashboards "HomeShop Overview" (A1/A2) and "HomeShop Load Balancing (A3)"
- **filebeat** — reads Docker's JSON log files and ships the app, worker and Envoy access-log events to Elasticsearch
- **elasticsearch** — indexes searchable logs in `homeshop-logs-*`
- **kibana** — explores and searches the stored logs

## Requirements

- Docker and Docker Compose

## Run it

```bash
docker compose up --build
```

The API will be available at http://localhost:8000. Interactive docs at
http://localhost:8000/docs.

Requests go through Envoy to three replicas. gRPC is on `localhost:50051`.
See the Assignment 2 and 3 sections below.

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

## Assignment 2: HTTP API, gRPC, and metrics

Full write-up and results: `ASSIGNMENT2_REPORT.md`. Start everything first with `docker compose up --build -d`, then run:

```bash
# Part A: HTTP API
python3 scripts/test_part_a.py            # valid / invalid / repeated / rate limit / OpenAPI contract
python3 scripts/part_a_http_caller.py     # timeout, retry + backoff, circuit breaker

# Part B: gRPC checkout
docker compose run --rm tools python -m grpc_service.client alice --deadline 3   # direct gRPC call (user ID as argument)
docker compose run --rm tools python scripts/test_part_b.py                     # success + failure over HTTP and gRPC

# Part C: populate the gRPC/HTTP checkout panels in Grafana
docker compose run --rm tools python scripts/run_grpc_metrics_demo.py --count 4
```

- OpenAPI spec: `openapi.json` (regenerate with `curl -s localhost:8000/openapi.json | python3 -m json.tool > openapi.json`). Swagger UI: http://localhost:8000/docs
- Regenerate the gRPC code after editing `grpc_service/checkout.proto`:
  `python -m pip install -r grpc-requirements.txt && python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. grpc_service/checkout.proto`
- gRPC listens on `localhost:50051` (through Envoy); each replica's gRPC metrics are on its own `/metrics` (job `homeshop-app`). The checkout comparison panels are at the bottom of Grafana's **HomeShop Overview** dashboard.
- The A2 reliability caller injects faults into one replica's memory, so point it at a single replica: `docker compose run --rm -e API_URL=http://homeshop-app-1:8000 tools python scripts/part_a_http_caller.py`

## Assignment 3: load balancing with Envoy

Full write-up and results: `ASSIGNMENT3_REPORT.md`. Dashboard: **HomeShop Load Balancing (A3)** — http://localhost:3000/d/homeshop-lb

**Setup**

```bash
docker compose up --build -d                        # 3 app replicas (deploy.replicas: 3) + Envoy (round robin)
docker compose up -d --scale app=3                  # equivalent explicit scaling
curl -s localhost:9901/clusters | grep health_flags # Envoy's view of A/B/C health
```

**Envoy policies.** The configs are generated by `python3 envoy/render.py`, one file per policy:

```bash
ENVOY_CONFIG=envoy-a-only.yaml        docker compose up -d envoy   # Part A/B: A only
ENVOY_CONFIG=envoy-round-robin.yaml   docker compose up -d envoy   # Part C: equal round robin (default)
ENVOY_CONFIG=envoy-weighted.yaml      docker compose up -d envoy   # Part D: A:B:C = 2:1:1
ENVOY_CONFIG=envoy-least-request.yaml docker compose up -d envoy   # Part D: least request
```

**Workload** (fixed rate, open loop, no retries; results are saved to `results/<label>.json`):

```bash
docker compose run --rm tools python scripts/loadtest.py --rate 30 --duration 60 --label my-run
```

**Experiments.** This runs every run in order (about 15 minutes) and restores round robin afterwards:

```bash
bash scripts/run_experiments.sh                 # a-only, round-robin, weighted, rr-delay, lr-delay, failure
bash scripts/run_experiments.sh lr-delay        # or a single run
```

**Faults and recovery (manual)**

```bash
docker compose run --rm tools python scripts/set_delay.py B 0.2   # add 200 ms to replica B
docker compose run --rm tools python scripts/set_delay.py B 0     # remove it
docker stop homeshop-app-2                                        # stop replica B
docker start homeshop-app-2                                       # restart it
ENVOY_CONFIG=envoy-round-robin.yaml docker compose up -d envoy    # back to normal routing
docker compose start simulator                                    # background traffic back on
```

**Log check.** In Kibana, search `request_id:"<id>"` (use an ID from `sample_request_id` in a results file). It returns Envoy's `envoy_access` event, with `replica` and `upstream_host`, together with the replica's own event.

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

## Metrics (Assignment 1, Part B)

Once `docker compose up --build` is running:

- **Prometheus UI**: http://localhost:9090 — check Status → Targets to confirm the scrape targets show as "UP": `homeshop-app` (A, B, C), `envoy`, `homeshop-worker`, `node-exporter` and `prometheus`.
- **Grafana**: http://localhost:3000 — log in with `admin` / `admin` (you'll be asked to set a new password, or you can skip). Open Dashboards → **HomeShop Overview**, which is provisioned automatically.
- **Raw metrics**: http://localhost:9901/stats/prometheus (Envoy) and http://localhost:8001/metrics (worker). Each replica's `/metrics` is only reachable inside the Docker network, where Prometheus scrapes it directly.

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

## Logging (Assignment 1, Part C)

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
