"""
A fake payment gateway.

This exists so we have a controllable, external-service-like dependency
to instrument (latency) and later break on purpose for the Part E
anomaly experiment (Assignment 1), e.g. "every 5th request is 500ms
slower" or "20% of payments fail".

Configuration is via environment variables so we can change behavior
without touching code / rebuilding the image:

  PAYMENT_BASE_LATENCY_MS   - normal processing time (default 80ms)
  PAYMENT_JITTER_MS         - +/- random jitter added to base latency
  PAYMENT_FAILURE_RATE      - 0.0-1.0 chance a payment is declined
  PAYMENT_EXTRA_DELAY_MS    - extra fixed delay to inject on demand (fault injection)
  PAYMENT_EXTRA_DELAY_EVERY_N - if set, add PAYMENT_EXTRA_DELAY_MS to every Nth call
"""
import os
import random
import time
import itertools

_call_counter = itertools.count(1)


class PaymentDeclined(Exception):
    pass


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def charge(amount_cents: int, user_id: str) -> dict:
    """
    Simulate charging a customer. Sleeps to simulate network/processing
    time, may raise PaymentDeclined to simulate a failed payment.
    Returns a fake transaction receipt on success.
    """
    base_ms = _env_float("PAYMENT_BASE_LATENCY_MS", 80)
    jitter_ms = _env_float("PAYMENT_JITTER_MS", 40)
    failure_rate = _env_float("PAYMENT_FAILURE_RATE", 0.03)
    extra_delay_ms = _env_float("PAYMENT_EXTRA_DELAY_MS", 0)
    extra_delay_every_n = _env_int("PAYMENT_EXTRA_DELAY_EVERY_N", 0)

    # Dynamic fault injection check from shared volume /tmp/fault_config.json
    fault_file = "/tmp/fault_config.json"
    if os.path.exists(fault_file):
        try:
            import json
            with open(fault_file, "r") as f:
                cfg = json.load(f)
                extra_delay_ms = cfg.get("extra_delay_ms", extra_delay_ms)
                extra_delay_every_n = cfg.get("extra_delay_every_n", extra_delay_every_n)
                failure_rate = cfg.get("failure_rate", failure_rate)
        except Exception:
            pass

    call_num = next(_call_counter)


    delay_ms = max(0.0, base_ms + random.uniform(-jitter_ms, jitter_ms))

    # Fault-injection hook: add extra delay to every Nth call.
    if extra_delay_every_n and call_num % extra_delay_every_n == 0:
        delay_ms += extra_delay_ms

    time.sleep(delay_ms / 1000.0)

    if random.random() < failure_rate:
        # User identifiers are neither needed to diagnose a decline nor safe
        # to write to logs (the caller logs a generic reason and order ID).
        raise PaymentDeclined("Card declined")

    return {
        "transaction_id": f"txn_{call_num}_{int(time.time() * 1000)}",
        "amount_cents": amount_cents,
        "status": "approved",
    }
