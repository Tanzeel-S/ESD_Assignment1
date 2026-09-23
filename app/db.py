import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .models import Base

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://homeshop:homeshop@db:5432/homeshop"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    # ``create_all`` does not modify tables that already exist. These two
    # additive migrations preserve assignment data created before seller
    # ownership was introduced and place it in a clearly named legacy store.
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS seller_id VARCHAR(32)"))
            connection.execute(text("UPDATE products SET seller_id = 'legacy-store' WHERE seller_id IS NULL"))
            connection.execute(text("ALTER TABLE products ALTER COLUMN seller_id SET NOT NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_products_seller_id ON products (seller_id)"))

            connection.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS seller_id VARCHAR(32)"))
            connection.execute(text("UPDATE orders SET seller_id = 'legacy-store' WHERE seller_id IS NULL"))
            connection.execute(text("ALTER TABLE orders ALTER COLUMN seller_id SET NOT NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_orders_seller_id ON orders (seller_id)"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
