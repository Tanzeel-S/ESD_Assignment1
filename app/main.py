"""
HomeShop API
------------
A minimal e-commerce backend aimed at home-based / small businesses
(e.g. someone selling candles, baked goods, or crafts from home).

Endpoints:
  POST   /products                        - add a product (admin/seller)
  GET    /products                        - browse products
  GET    /products/{product_id}           - product detail
  POST   /products/{product_id}/restock   - add stock to a product
  POST   /cart/{user_id}/add              - add an item to a user's cart
  GET    /cart/{user_id}                  - view a user's cart
  PATCH  /cart/{user_id}/item/{product_id}- update quantity of a cart item
  DELETE /cart/{user_id}/item/{product_id}- remove an item from the cart
  POST   /checkout/{user_id}              - place an order from the cart
  GET    /orders/{order_id}               - check order status
  GET    /orders?user_id=...              - list orders (all, or for one user)
  POST   /orders/{order_id}/cancel        - cancel a pending order

Part B adds Prometheus metrics: HTTP request Counter/Histogram via
middleware, plus business Counters/Gauge on the endpoints below.
See /metrics for the raw exposition format Prometheus scrapes.
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Depends, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .db import SessionLocal, get_db, init_db
from .catalog import ProductNotFound, get_product as find_product
from .replica import INJECTED_DELAY, REPLICA_ID
from .auth import current_user, require_same_user
from .checkout import CheckoutRuleError, create_order_from_cart
from .models import Product, CartItem, Order, OrderItem, OrderStatus, IdempotencyRecord, RateLimitEvent
from .metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_FLIGHT,
    REPLICA_INFO,
    ORDERS_PLACED_TOTAL,
    ORDERS_CANCELLED_TOTAL,
    CHECKOUT_REJECTED_TOTAL,
    INVENTORY_STOCK,
    CARDINALITY_TEST_TOTAL,
)
from .observability_logging import configure_logging, log_event, request_id_var

STATIC_DIR = Path(__file__).parent / "static"

class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_doc(description: str) -> dict:
    return {"model": ErrorResponse, "description": description}


app = FastAPI(
    title="HomeShop API",
    version="2.0.0",
    # Every route can fail with the shared error format below.
    responses={
        400: error_doc("Invalid input or business-rule rejection"),
        422: error_doc("Request validation failed"),
        500: error_doc("Unexpected server error"),
    },
)
logger = logging.getLogger("homeshop.api")

CHECKOUT_RATE_LIMIT_PER_MINUTE = 10
CHECKOUT_RATE_LIMIT_WINDOW_SECONDS = 60
NOT_APPLICATION_PATHS = ("/metrics", "/healthz", "/debug/delay")


def error_payload(code: str, message: str, details: object | None = None) -> dict:
    payload = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return payload


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", "http_error")
        message = detail.get("message", "Request failed")
        details = detail.get("details")
    elif isinstance(detail, str):
        code = "http_error"
        message = detail
        details = None
    else:
        code = "http_error"
        message = "Request failed"
        details = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(code, message, details),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=error_payload(
            "validation_error",
            "Request validation failed",
            [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()],
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled API exception", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_server_error", "The server encountered an unexpected error"),
    )


GRPC_SERVER = {}


@app.on_event("startup")
def on_startup():
    configure_logging()
    init_db()
    REPLICA_INFO.labels(replica=REPLICA_ID).set(1)
    # Each replica serves HTTP (uvicorn, :8000) and gRPC (:50051) from one
    # process, so both interfaces share one /metrics registry and one identity.
    from grpc_service.server import start_grpc_server

    GRPC_SERVER["server"] = start_grpc_server()
    log_event(logger, logging.INFO, "api_started")


@app.on_event("shutdown")
def on_shutdown():
    if GRPC_SERVER.get("server"):
        GRPC_SERVER["server"].stop(grace=2)


def _rate_limit_retry_after(bucket_key: str) -> int | None:
    """Shared sliding window in Postgres: every replica counts the same calls.

    Returns seconds to wait if the bucket is full, otherwise records the call.
    A per-bucket advisory lock stops two replicas admitting the 11th call at once.
    """
    now = datetime.utcnow()
    window = timedelta(seconds=CHECKOUT_RATE_LIMIT_WINDOW_SECONDS)
    with SessionLocal() as db, db.begin():
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:bucket))"), {"bucket": bucket_key})
        db.query(RateLimitEvent).filter(
            RateLimitEvent.bucket == bucket_key, RateLimitEvent.created_at <= now - window
        ).delete()
        times = [
            row[0]
            for row in db.query(RateLimitEvent.created_at)
            .filter(RateLimitEvent.bucket == bucket_key)
            .order_by(RateLimitEvent.created_at)
            .all()
        ]
        if len(times) >= CHECKOUT_RATE_LIMIT_PER_MINUTE:
            return max(1, int((times[0] + window - now).total_seconds()) + 1)
        db.add(RateLimitEvent(bucket=bucket_key, created_at=now))
    return None


async def checkout_rate_limit_response(request: Request) -> JSONResponse | None:
    """Sliding-window limit: 10 checkout calls per 60 s per (user, checkout path), shared by all replicas."""
    if not (request.method == "POST" and request.url.path.startswith("/checkout/")):
        return None
    actor = request.headers.get("X-HomeShop-User") or (request.client.host if request.client else "anonymous")
    bucket_key = f"{actor.strip().lower()}:{request.url.path}"
    retry_after = await run_in_threadpool(_rate_limit_retry_after, bucket_key)
    if retry_after is not None:
        log_event(
            logger,
            logging.WARNING,
            "checkout_rate_limited",
            path="/checkout/{user_id}",
            retry_after_seconds=retry_after,
        )
        return JSONResponse(
            status_code=429,
            content=error_payload(
                "rate_limited",
                "Too many checkout requests; please wait before retrying.",
                {"retry_after_seconds": retry_after, "limit": CHECKOUT_RATE_LIMIT_PER_MINUTE,
                 "window_seconds": CHECKOUT_RATE_LIMIT_WINDOW_SECONDS},
            ),
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(CHECKOUT_RATE_LIMIT_PER_MINUTE),
                "X-RateLimit-Remaining": "0",
            },
        )
    return None


@app.middleware("http")
async def track_http_metrics(request: Request, call_next):
    """
    Times every request and records it under the *route template*
    (e.g. /orders/{order_id}) rather than the raw path, so that
    per-order-id paths don't create a separate time series per order.
    This is the same cardinality discipline we deliberately break later
    for the Part E.2 demo.
    """
    supplied_request_id = request.headers.get("X-Request-ID")
    try:
        # Accept only UUIDs from callers: arbitrary header values may contain
        # personal data and would otherwise be copied into the log stream.
        request_id = str(uuid.UUID(supplied_request_id)) if supplied_request_id else str(uuid.uuid4())
    except ValueError:
        request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    is_application = request.url.path not in NOT_APPLICATION_PATHS
    if is_application:
        HTTP_REQUESTS_IN_FLIGHT.inc()
    start = time.perf_counter()
    try:
        if is_application and INJECTED_DELAY["seconds"] > 0:
            # Assignment 3 Part D: make this replica slow (health checks unaffected).
            await asyncio.sleep(INJECTED_DELAY["seconds"])
        # Rate-limit after the request ID is assigned so 429s are logged,
        # carry X-Request-ID, and are counted in the HTTP metrics.
        limited = await checkout_rate_limit_response(request)
        response = limited or await call_next(request)
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        if limited is not None:
            path_template = "/checkout/{user_id}"
        else:
            path_template = route.path if route else request.url.path

        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method, path=path_template
        ).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path_template, status=response.status_code
        ).inc()
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Replica"] = REPLICA_ID
        if request.url.path == "/healthz":
            return response  # health checks every 2 s would flood the logs
        log_event(
            logger,
            logging.INFO,
            "http_request_complete",
            method=request.method,
            path=path_template,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response
    except Exception as error:
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        path_template = route.path if route else "unmatched"
        # Unhandled errors become 500s in the outer error handler; count them here.
        HTTP_REQUEST_DURATION_SECONDS.labels(method=request.method, path=path_template).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(method=request.method, path=path_template, status=500).inc()
        log_event(
            logger,
            logging.ERROR,
            "http_request_unhandled_error",
            method=request.method,
            path=path_template,
            duration_ms=round(duration * 1000, 2),
            error_type=type(error).__name__,
        )
        raise
    finally:
        if is_application:
            HTTP_REQUESTS_IN_FLIGHT.dec()
        request_id_var.reset(token)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/healthz")
def healthz():
    """Envoy active health check. Deliberately cheap: a slow replica still passes."""
    return {"status": "ok", "replica": REPLICA_ID}


class DelayIn(BaseModel):
    seconds: float = Field(ge=0, le=5)


@app.post("/debug/delay")
def set_delay(payload: DelayIn):
    """Demo only (A3 Part D): add a fixed delay to every application request on THIS replica."""
    INJECTED_DELAY["seconds"] = payload.seconds
    log_event(logger, logging.WARNING, "injected_delay_set", seconds=payload.seconds)
    return {"replica": REPLICA_ID, "delay_seconds": payload.seconds}


# ---------- Schemas ----------

class ProductIn(BaseModel):
    name: str
    description: str = ""
    price_cents: int = Field(gt=0)
    stock: int = Field(ge=0, default=0)


class ProductOut(BaseModel):
    id: str
    name: str
    description: str
    price_cents: int
    stock: int
    seller_id: str

    class Config:
        from_attributes = True


class CartAddIn(BaseModel):
    product_id: str
    quantity: int = Field(gt=0, default=1)


class OrderOut(BaseModel):
    id: str
    user_id: str
    seller_id: str
    status: OrderStatus
    total_cents: int
    failure_reason: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------- Products ----------

@app.post("/products", response_model=ProductOut)
def create_product(
    payload: ProductIn,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    product = Product(**payload.model_dump(), seller_id=actor)
    db.add(product)
    db.commit()
    db.refresh(product)
    INVENTORY_STOCK.labels(product_name=product.name, seller_id=product.seller_id).set(product.stock)
    log_event(logger, logging.INFO, "product_created", product_id=product.id, stock=product.stock)
    return product


@app.post("/products/{product_id}/restock", response_model=ProductOut)
def restock_product(
    product_id: str,
    quantity: int,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    """Add stock to an existing product. Lets the inventory Gauge go back up,
    not just down, which is a more convincing demo in Grafana."""
    if quantity <= 0:
        raise HTTPException(400, "Quantity must be positive")
    product = db.query(Product).get(product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    if product.seller_id != actor:
        raise HTTPException(403, "Only the seller who listed this product can restock it")
    product.stock += quantity
    db.commit()
    db.refresh(product)
    INVENTORY_STOCK.labels(product_name=product.name, seller_id=product.seller_id).set(product.stock)
    log_event(logger, logging.INFO, "product_restocked", product_id=product.id, stock=product.stock)
    return product


@app.get("/products", response_model=list[ProductOut])
def list_products(seller_id: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Product)
    if seller_id:
        query = query.filter(Product.seller_id == seller_id.lower())
    return query.order_by(Product.created_at.desc()).all()


@app.get("/products/mine", response_model=list[ProductOut])
def list_my_products(
    db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    return (
        db.query(Product)
        .filter(Product.seller_id == actor)
        .order_by(Product.created_at.desc())
        .all()
    )


@app.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: str, db: Session = Depends(get_db)):
    try:
        return find_product(db, product_id)
    except ProductNotFound:
        raise HTTPException(404, "Product not found") from None


# ---------- Cart ----------

@app.post("/cart/{user_id}/add")
def add_to_cart(
    user_id: str,
    payload: CartAddIn,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    require_same_user(actor, user_id)
    product = db.query(Product).get(payload.product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    existing_cart_item = db.query(CartItem).filter_by(user_id=actor).first()
    if existing_cart_item and existing_cart_item.product.seller_id != product.seller_id:
        raise HTTPException(
            400,
            "Your cart contains items from another seller. Check out or remove them first.",
        )

    item = (
        db.query(CartItem)
        .filter_by(user_id=actor, product_id=payload.product_id)
        .first()
    )
    current_quantity = item.quantity if item else 0
    if product.stock < current_quantity + payload.quantity:
        raise HTTPException(400, "Not enough stock")
    if item:
        item.quantity += payload.quantity
    else:
        item = CartItem(
            user_id=actor,
            product_id=payload.product_id,
            quantity=payload.quantity,
        )
        db.add(item)
    db.commit()
    return {"status": "ok"}


@app.get("/cart/{user_id}")
def view_cart(
    user_id: str, db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    require_same_user(actor, user_id)
    items = db.query(CartItem).filter_by(user_id=actor).all()
    return [
        {
            "product_id": i.product_id,
            "name": i.product.name,
            "quantity": i.quantity,
            "unit_price_cents": i.product.price_cents,
            "stock": i.product.stock,
            "seller_id": i.product.seller_id,
        }
        for i in items
    ]


class CartUpdateIn(BaseModel):
    quantity: int = Field(gt=0)


@app.patch("/cart/{user_id}/item/{product_id}")
def update_cart_item(
    user_id: str,
    product_id: str,
    payload: CartUpdateIn,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    require_same_user(actor, user_id)
    item = db.query(CartItem).filter_by(user_id=actor, product_id=product_id).first()
    if not item:
        raise HTTPException(404, "Item not in cart")
    if item.product.stock < payload.quantity:
        raise HTTPException(400, "Not enough stock")
    item.quantity = payload.quantity
    db.commit()
    return {"status": "ok"}


@app.delete("/cart/{user_id}/item/{product_id}")
def remove_cart_item(
    user_id: str,
    product_id: str,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    require_same_user(actor, user_id)
    item = db.query(CartItem).filter_by(user_id=actor, product_id=product_id).first()
    if not item:
        raise HTTPException(404, "Item not in cart")
    db.delete(item)
    db.commit()
    return {"status": "ok"}


# ---------- Checkout / Orders ----------

CHECKOUT_FAULT = {"mode": "off", "remaining": 0}


@app.post(
    "/checkout/{user_id}",
    response_model=OrderOut,
    responses={
        403: error_doc("Caller may not check out another user's cart"),
        409: error_doc("A checkout with this Idempotency-Key is still in progress"),
        429: {
            "model": ErrorResponse,
            "description": "Checkout rate limit exceeded (10 calls / 60 s per user)",
            "headers": {"Retry-After": {"description": "Seconds until a call is allowed", "schema": {"type": "integer"}}},
        },
        503: error_doc("Injected transient failure (reliability demo only)"),
    },
)
def checkout(
    user_id: str,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Client-chosen key; repeating it returns the original order instead of creating another.",
    ),
):
    require_same_user(actor, user_id)
    if CHECKOUT_FAULT["remaining"] > 0:
        # Reliability demo: fail before any business logic, so no side effects.
        CHECKOUT_FAULT["remaining"] -= 1
        if CHECKOUT_FAULT["mode"] == "slow":
            time.sleep(3)
        raise HTTPException(503, {"code": "transient_failure", "message": "Injected checkout failure; retry later."})
    idem_key = (idempotency_key or "").strip()
    key = f"{actor}:{idem_key}"
    if idem_key:
        # Shared by all replicas: the primary key makes "claim this key" atomic,
        # so a retry routed to a different replica still finds the first attempt.
        try:
            db.add(IdempotencyRecord(key=key, status="in_progress"))
            db.commit()
        except IntegrityError:
            db.rollback()
            previous = db.get(IdempotencyRecord, key)
            if previous and previous.status == "completed":
                order = db.get(Order, previous.order_id)
                if order is not None:
                    return order
            raise HTTPException(
                409,
                {"code": "idempotency_conflict", "message": "This checkout is already in progress for this idempotency key."},
            ) from None

    try:
        order = create_order_from_cart(db, actor)
        if idem_key:
            record = db.get(IdempotencyRecord, key)
            record.status, record.order_id = "completed", order.id
            db.commit()
        return order
    except Exception as error:
        db.rollback()
        if idem_key:
            # Release the key so the client may retry a failed checkout.
            db.query(IdempotencyRecord).filter_by(key=key).delete()
            db.commit()
        if isinstance(error, CheckoutRuleError):
            raise HTTPException(
                error.status_code,
                {"code": error.code, "message": error.message},
            ) from error
        raise


@app.get("/seller/orders", response_model=list[OrderOut])
def list_seller_orders(
    db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    return (
        db.query(Order)
        .filter(Order.seller_id == actor)
        .order_by(Order.created_at.desc())
        .all()
    )


@app.get("/orders/{order_id}", response_model=OrderOut)
def get_order(
    order_id: str, db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    order = db.query(Order).get(order_id)
    if not order or actor not in (order.user_id, order.seller_id):
        raise HTTPException(404, "Order not found")
    return order


@app.get("/orders", response_model=list[OrderOut])
def list_orders(
    user_id: str | None = None,
    db: Session = Depends(get_db),
    actor: str = Depends(current_user),
):
    """Return the selected customer's order history, never all customers' orders."""
    if user_id:
        require_same_user(actor, user_id)
    return db.query(Order).filter(Order.user_id == actor).order_by(Order.created_at.desc()).all()


@app.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel_order(
    order_id: str, db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    order = db.query(Order).get(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.user_id != actor:
        raise HTTPException(403, "Only the customer who placed this order can cancel it")
    if order.status not in (OrderStatus.PENDING, OrderStatus.PROCESSING):
        raise HTTPException(400, f"Cannot cancel order in status {order.status}")
    order.status = OrderStatus.CANCELLED
    db.commit()
    db.refresh(order)
    ORDERS_CANCELLED_TOTAL.inc()
    log_event(logger, logging.INFO, "order_cancelled", order_id=order.id)
    return order


# ---------- Experiments (Part E) ----------

@app.post("/experiment/cardinality/run")
def run_cardinality_experiment(count: int = 100):
    """
    Part E.2: Cardinality Explosion Experiment.
    Generates `count` unique request IDs as label values on a counter metric,
    demonstrating time-series growth (cardinality explosion) in Prometheus.
    """
    if count < 1 or count > 500:
        raise HTTPException(400, "Count must be between 1 and 500")
    
    generated_ids = []
    for _ in range(count):
        req_id = f"test-req-{uuid.uuid4().hex[:12]}"
        CARDINALITY_TEST_TOTAL.labels(request_id=req_id).inc()
        log_event(
            logger,
            logging.INFO,
            "cardinality_experiment_sample",
            sample_request_id=req_id,
            experiment="part_e2_cardinality",
        )
        generated_ids.append(req_id)

    log_event(
        logger,
        logging.WARNING,
        "cardinality_explosion_triggered",
        count=count,
        metric="cardinality_test_total",
    )
    return {
        "status": "ok",
        "unique_ids_generated": count,
        "metric": "cardinality_test_total",
        "sample_ids": generated_ids[:5],
        "message": f"Successfully created {count} unique time series for cardinality explosion test.",
    }


class CheckoutFaultIn(BaseModel):
    mode: str = Field(pattern="^(error|slow|off)$")
    count: int = Field(ge=0, le=100, default=1)


@app.post("/debug/checkout-fault")
def set_checkout_fault(payload: CheckoutFaultIn):
    """Demo only: make the next `count` checkout calls fail fast (503) or slowly (3 s, then 503)."""
    CHECKOUT_FAULT["mode"] = payload.mode
    CHECKOUT_FAULT["remaining"] = 0 if payload.mode == "off" else payload.count
    return dict(CHECKOUT_FAULT)


# ---------- Frontend ----------
# Mounted LAST, at "/", so every API route above is matched first and
# this only serves the static multi-page site (html=True serves
# index.html for "/" and any unmatched path falls through to 404,
# not to an API route).
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

