"""
HomeShop Traffic Simulator
--------------------------
Generates realistic user and seller traffic to ensure Prometheus metrics and
Elasticsearch/Kibana logs show dynamic, live curves and active log events
rather than flat lines.
"""
import os
import time
import random
import logging
import requests

API_URL = os.getenv("API_URL", "http://api:8000")
POLL_INTERVAL = float(os.getenv("SIMULATOR_INTERVAL", "1.0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("homeshop.simulator")

CUSTOMERS = ["alice", "bob", "charlie", "diana", "eve"]
SELLERS = ["niha", "alice", "bob"]

INITIAL_PRODUCTS = [
    {
        "seller_id": "niha",
        "name": "Hand-poured Lavender Candle",
        "description": "Natural soy wax candle with essential oils",
        "price_cents": 1200,
        "stock": 50,
    },
    {
        "seller_id": "alice",
        "name": "Artisan Sourdough Loaf",
        "description": "Freshly baked organic sourdough bread",
        "price_cents": 850,
        "stock": 40,
    },
    {
        "seller_id": "bob",
        "name": "Handcrafted Ceramic Mug",
        "description": "Wheel-thrown earthenware pottery mug",
        "price_cents": 1800,
        "stock": 35,
    },
    {
        "seller_id": "niha",
        "name": "Organic Honey Jar",
        "description": "Raw wildflower honey from local hives",
        "price_cents": 1500,
        "stock": 45,
    },
]


def wait_for_api():
    """Wait until the API is available."""
    logger.info("Waiting for HomeShop API at %s...", API_URL)
    while True:
        try:
            r = requests.get(f"{API_URL}/products", timeout=3)
            if r.status_code == 200:
                logger.info("Connected to HomeShop API successfully.")
                return
        except Exception:
            pass
        time.sleep(2)


def seed_products():
    """Ensure baseline products exist for sellers."""
    try:
        r = requests.get(f"{API_URL}/products")
        existing = r.json() if r.status_code == 200 else []
        if len(existing) >= len(INITIAL_PRODUCTS):
            return existing

        logger.info("Seeding initial marketplace products...")
        for p in INITIAL_PRODUCTS:
            headers = {"X-HomeShop-User": p["seller_id"]}
            payload = {
                "name": p["name"],
                "description": p["description"],
                "price_cents": p["price_cents"],
                "stock": p["stock"],
            }
            requests.post(f"{API_URL}/products", json=payload, headers=headers)
        
        r = requests.get(f"{API_URL}/products")
        return r.json()
    except Exception as e:
        logger.warning("Error seeding products: %s", e)
        return []


def run_simulator():
    wait_for_api()
    products = seed_products()

    logger.info("Traffic simulator started. Generating dynamic background load...")

    order_history = []

    while True:
        try:
            # Refresh products list periodically
            if random.random() < 0.1 or not products:
                r = requests.get(f"{API_URL}/products")
                if r.status_code == 200 and r.json():
                    products = r.json()

            if not products:
                time.sleep(2)
                continue

            customer = random.choice(CUSTOMERS)
            headers = {"X-HomeShop-User": customer}

            action_roll = random.random()

            if action_roll < 0.35:
                # Action 1: Browse marketplace & view product details
                p = random.choice(products)
                requests.get(f"{API_URL}/products", headers=headers)
                requests.get(f"{API_URL}/products/{p['id']}", headers=headers)

            elif action_roll < 0.75:
                # Action 2: Add item to cart & Checkout
                p = random.choice(products)
                # First clear existing cart if any, to avoid multi-seller error
                r_cart = requests.get(f"{API_URL}/cart/{customer}", headers=headers)
                if r_cart.status_code == 200:
                    for item in r_cart.json():
                        requests.delete(
                            f"{API_URL}/cart/{customer}/item/{item['product_id']}",
                            headers=headers,
                        )

                # Add selected item
                add_resp = requests.post(
                    f"{API_URL}/cart/{customer}/add",
                    json={"product_id": p["id"], "quantity": random.randint(1, 2)},
                    headers=headers,
                )

                if add_resp.status_code == 200:
                    checkout_resp = requests.post(
                        f"{API_URL}/checkout/{customer}", headers=headers
                    )
                    if checkout_resp.status_code == 200:
                        order = checkout_resp.json()
                        order_history.append(order["id"])
                        if len(order_history) > 50:
                            order_history.pop(0)

            elif action_roll < 0.88:
                # Action 3: Check order status
                if order_history:
                    oid = random.choice(order_history)
                    requests.get(f"{API_URL}/orders/{oid}", headers=headers)
                else:
                    requests.get(f"{API_URL}/orders", headers=headers)

            elif action_roll < 0.94:
                # Action 4: Cancel a pending order
                if order_history:
                    oid = random.choice(order_history)
                    requests.post(f"{API_URL}/orders/{oid}/cancel", headers=headers)

            else:
                # Action 5: Seller restocks a product
                p = random.choice(products)
                seller = p.get("seller_id", "niha")
                requests.post(
                    f"{API_URL}/products/{p['id']}/restock?quantity=15",
                    headers={"X-HomeShop-User": seller},
                )

        except Exception as err:
            logger.debug("Simulator iteration error: %s", err)

        time.sleep(random.uniform(POLL_INTERVAL * 0.5, POLL_INTERVAL * 1.5))


if __name__ == "__main__":
    run_simulator()
