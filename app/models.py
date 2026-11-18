"""
SQLAlchemy models for HomeShop: a tiny e-commerce backend for
home-based / small businesses (e.g. someone selling candles or
baked goods out of their house).
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def gen_id():
    return str(uuid.uuid4())


class OrderStatus(str, enum.Enum):
    PENDING = "pending"          # created, waiting for worker to process
    PROCESSING = "processing"    # worker picked it up, calling payment gateway
    COMPLETED = "completed"      # payment succeeded, order fulfilled
    FAILED = "failed"            # payment failed
    CANCELLED = "cancelled"      # cancelled by customer/admin


class Product(Base):
    __tablename__ = "products"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    description = Column(String, default="")
    price_cents = Column(Integer, nullable=False)  # store money as integer cents
    stock = Column(Integer, nullable=False, default=0)
    seller_id = Column(String(32), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, nullable=False, index=True)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)

    product = relationship("Product")


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, nullable=False, index=True)
    seller_id = Column(String(32), nullable=False, index=True)
    status = Column(SAEnum(OrderStatus), default=OrderStatus.PENDING, index=True)
    total_cents = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    failure_reason = Column(String, nullable=True)

    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(String, primary_key=True, default=gen_id)
    order_id = Column(String, ForeignKey("orders.id"), nullable=False)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price_cents = Column(Integer, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")


class IdempotencyRecord(Base):
    """Shared by all replicas, so a repeated key is recognised wherever it lands."""
    __tablename__ = "idempotency_records"

    key = Column(String, primary_key=True)          # "<user_id>:<Idempotency-Key>"
    status = Column(String, nullable=False)         # in_progress | completed
    order_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RateLimitEvent(Base):
    """One row per accepted checkout call; the sliding window is counted across replicas."""
    __tablename__ = "rate_limit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bucket = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, index=True)
