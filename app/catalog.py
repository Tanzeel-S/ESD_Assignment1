"""Shared read-only product lookup used by HTTP GET /products/{id} and gRPC GetProduct."""
from sqlalchemy.orm import Session

from .models import Product


class ProductNotFound(Exception):
    pass


def get_product(db: Session, product_id: str) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFound(product_id)
    return product
