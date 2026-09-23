"""
HomeShop order worker.

Polls the database for PENDING orders, "processes" them by calling the
simulated payment gateway, and marks them COMPLETED or FAILED.

Kept as a simple polling loop (rather than a real queue like
RabbitMQ/Redis) to keep Part A simple, per the assignment's own
"as simple or complex as you like" guidance. This is exactly the kind
of component we'll add a queue-depth Gauge and processing-time
Histogram to in Part B.
"""
import time
import sys
import os
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, init_db
from app.models import Order, OrderStatus
from app.observability_logging import configure_logging, log_event
from payment_gateway import charge, PaymentDeclined
from metrics import (
    ORDERS_COMPLETED_TOTAL,
    ORDERS_FAILED_TOTAL,
    REVENUE_CENTS_TOTAL,
    ORDERS_QUEUE_DEPTH,
    PAYMENT_PROCESSING_SECONDS,
    ORDER_FULFILLMENT_SECONDS,
    start_metrics_server,
)

POLL_INTERVAL_SECONDS = float(os.getenv("WORKER_POLL_INTERVAL", "1.0"))
METRICS_PORT = int(os.getenv("WORKER_METRICS_PORT", "8001"))
logger = logging.getLogger("homeshop.worker")


def process_one_order(db, order: Order):
    order.status = OrderStatus.PROCESSING
    db.commit()
    log_event(logger, logging.INFO, "order_processing_started", order_id=order.id)

    with PAYMENT_PROCESSING_SECONDS.time():
        try:
            receipt = charge(order.total_cents, order.user_id)
            payment_ok = True
        except PaymentDeclined as e:
            payment_ok = False
            failure_reason = str(e)

    fulfillment_seconds = (datetime.utcnow() - order.created_at).total_seconds()
    ORDER_FULFILLMENT_SECONDS.observe(fulfillment_seconds)

    # A customer can cancel while the simulated gateway call is in flight.
    # Refresh before writing an outcome so the worker never resurrects a
    # cancelled order as completed/failed.
    db.refresh(order)
    if order.status == OrderStatus.CANCELLED:
        log_event(
            logger,
            logging.INFO,
            "order_processing_cancelled",
            order_id=order.id,
        )
        return

    if payment_ok:
        order.status = OrderStatus.COMPLETED
        order.failure_reason = None
        ORDERS_COMPLETED_TOTAL.inc()
        REVENUE_CENTS_TOTAL.inc(order.total_cents)
        log_event(
            logger,
            logging.INFO,
            "order_completed",
            order_id=order.id,
            transaction_id=receipt["transaction_id"],
            total_cents=order.total_cents,
            fulfillment_seconds=round(fulfillment_seconds, 3),
        )
    else:
        order.status = OrderStatus.FAILED
        order.failure_reason = failure_reason
        ORDERS_FAILED_TOTAL.labels(reason="payment_declined").inc()
        log_event(
            logger,
            logging.WARNING,
            "order_failed",
            order_id=order.id,
            reason="payment_declined",
            fulfillment_seconds=round(fulfillment_seconds, 3),
        )

    db.commit()


def run():
    configure_logging()
    init_db()
    start_metrics_server(METRICS_PORT)
    log_event(
        logger,
        logging.INFO,
        "worker_started",
        metrics_port=METRICS_PORT,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )
    while True:
        db = SessionLocal()
        try:
            active = (
                db.query(Order)
                .filter(Order.status.in_([OrderStatus.PENDING, OrderStatus.PROCESSING]))
                .all()
            )
            ORDERS_QUEUE_DEPTH.set(len(active))

            pending = [o for o in active if o.status == OrderStatus.PENDING]
            for order in pending:
                process_one_order(db, order)
                # queue depth just changed; reflect it before the next sleep
                remaining = (
                    db.query(Order)
                    .filter(Order.status.in_([OrderStatus.PENDING, OrderStatus.PROCESSING]))
                    .count()
                )
                ORDERS_QUEUE_DEPTH.set(remaining)
        finally:
            db.close()
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
