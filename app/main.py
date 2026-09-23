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
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .db import get_db, init_db
from .auth import current_user, require_same_user
from .models import Product, CartItem, Order, OrderItem, OrderStatus
from .metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    ORDERS_PLACED_TOTAL,
    ORDERS_CANCELLED_TOTAL,
    CHECKOUT_REJECTED_TOTAL,
    INVENTORY_STOCK,
    CARDINALITY_TEST_TOTAL,
)
from .observability_logging import configure_logging, log_event, request_id_var

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="HomeShop API")
logger = logging.getLogger("homeshop.api")


@app.on_event("startup")
def on_startup():
    configure_logging()
    init_db()
    log_event(logger, logging.INFO, "api_started")


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
    start = time.perf_counter()
    try:
        response = await call_next(request)
        duration = time.perf_counter() - start
        route = request.scope.get("route")
        path_template = route.path if route else request.url.path

        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method, path=path_template
        ).observe(duration)
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=path_template, status=response.status_code
        ).inc()
        response.headers["X-Request-ID"] = request_id
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
        log_event(
            logger,
            logging.ERROR,
            "http_request_unhandled_error",
            method=request.method,
            path=route.path if route else "unmatched",
            duration_ms=round(duration * 1000, 2),
            error_type=type(error).__name__,
        )
        raise
    finally:
        request_id_var.reset(token)


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


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
    product = db.query(Product).get(product_id)
    if not product:
        raise HTTPException(404, "Product not found")
    return product


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

@app.post("/checkout/{user_id}", response_model=OrderOut)
def checkout(
    user_id: str, db: Session = Depends(get_db), actor: str = Depends(current_user)
):
    require_same_user(actor, user_id)
    cart_items = db.query(CartItem).filter_by(user_id=actor).with_for_update().all()
    if not cart_items:
        CHECKOUT_REJECTED_TOTAL.labels(reason="empty_cart").inc()
        log_event(logger, logging.WARNING, "checkout_rejected", reason="empty_cart")
        raise HTTPException(400, "Cart is empty")

    # Lock products during stock validation and decrement so simultaneous
    # checkouts cannot oversell the same small-batch item.
    product_ids = sorted({ci.product_id for ci in cart_items})
    locked_products = {
        product.id: product
        for product in db.query(Product).filter(Product.id.in_(product_ids)).with_for_update().all()
    }
    seller_ids = {locked_products[ci.product_id].seller_id for ci in cart_items}
    if len(seller_ids) != 1:
        raise HTTPException(400, "A checkout can contain products from one seller only")

    # Verify stock and compute total before committing anything.
    for ci in cart_items:
        product = locked_products[ci.product_id]
        if product.stock < ci.quantity:
            CHECKOUT_REJECTED_TOTAL.labels(reason="out_of_stock").inc()
            log_event(logger, logging.WARNING, "checkout_rejected", reason="out_of_stock")
            raise HTTPException(400, f"Not enough stock for {product.name}")

    order = Order(
        user_id=actor,
        seller_id=seller_ids.pop(),
        status=OrderStatus.PENDING,
        total_cents=0,
    )
    db.add(order)
    db.flush()  # get order.id

    total = 0
    for ci in cart_items:
        product = locked_products[ci.product_id]
        line_total = product.price_cents * ci.quantity
        total += line_total
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=ci.product_id,
                quantity=ci.quantity,
                unit_price_cents=product.price_cents,
            )
        )
        product.stock -= ci.quantity
        INVENTORY_STOCK.labels(product_name=product.name, seller_id=product.seller_id).set(product.stock)
        db.delete(ci)

    order.total_cents = total
    db.commit()
    db.refresh(order)
    ORDERS_PLACED_TOTAL.inc()
    log_event(logger, logging.INFO, "order_placed", order_id=order.id, total_cents=order.total_cents)
    return order


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


# ---------- Frontend ----------
# Mounted LAST, at "/", so every API route above is matched first and
# this only serves the static multi-page site (html=True serves
# index.html for "/" and any unmatched path falls through to 404,
# not to an API route).
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")

