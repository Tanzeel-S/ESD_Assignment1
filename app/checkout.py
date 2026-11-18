"""Shared checkout rules used by the HTTP API and the gRPC service."""

import logging

from sqlalchemy.orm import Session

from .metrics import CHECKOUT_REJECTED_TOTAL, INVENTORY_STOCK, ORDERS_PLACED_TOTAL
from .models import CartItem, Order, OrderItem, OrderStatus, Product
from .observability_logging import log_event

logger = logging.getLogger("homeshop.checkout")


class CheckoutRuleError(Exception):
    """A business-rule rejection that transports can map to their own status."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def create_order_from_cart(db: Session, actor: str) -> Order:
    """Apply the shared cart, stock, order, and inventory checkout rules."""
    cart_items = db.query(CartItem).filter_by(user_id=actor).with_for_update().all()
    if not cart_items:
        CHECKOUT_REJECTED_TOTAL.labels(reason="empty_cart").inc()
        log_event(logger, logging.WARNING, "checkout_rejected", reason="empty_cart")
        raise CheckoutRuleError("empty_cart", "Cart is empty")

    product_ids = sorted({item.product_id for item in cart_items})
    locked_products = {
        product.id: product
        for product in db.query(Product)
        .filter(Product.id.in_(product_ids))
        .with_for_update()
        .all()
    }
    seller_ids = {locked_products[item.product_id].seller_id for item in cart_items}
    if len(seller_ids) != 1:
        raise CheckoutRuleError(
            "multiple_sellers", "A checkout can contain products from one seller only"
        )

    for item in cart_items:
        product = locked_products[item.product_id]
        if product.stock < item.quantity:
            CHECKOUT_REJECTED_TOTAL.labels(reason="out_of_stock").inc()
            log_event(logger, logging.WARNING, "checkout_rejected", reason="out_of_stock")
            raise CheckoutRuleError("out_of_stock", f"Not enough stock for {product.name}")

    order = Order(
        user_id=actor,
        seller_id=seller_ids.pop(),
        status=OrderStatus.PENDING,
        total_cents=0,
    )
    db.add(order)
    db.flush()

    total = 0
    for item in cart_items:
        product = locked_products[item.product_id]
        total += product.price_cents * item.quantity
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price_cents=product.price_cents,
            )
        )
        product.stock -= item.quantity
        INVENTORY_STOCK.labels(
            product_name=product.name, seller_id=product.seller_id
        ).set(product.stock)
        db.delete(item)

    order.total_cents = total
    db.commit()
    db.refresh(order)
    ORDERS_PLACED_TOTAL.inc()
    log_event(
        logger,
        logging.INFO,
        "order_placed",
        order_id=order.id,
        total_cents=order.total_cents,
    )
    return order
