"""HomeShop gRPC service: Checkout (A2) and GetProduct (A3 test traffic).

Assignment 3: each replica runs this server inside its API process
(``start_grpc_server`` is called from the FastAPI startup hook), so HTTP and
gRPC share one replica identity and one Prometheus registry. ``python -m
grpc_service.server`` still runs it standalone.
"""

import logging
import os
import time
import uuid
from concurrent import futures
from datetime import timezone
from time import perf_counter

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from app.auth import validate_user_id
from app.catalog import ProductNotFound, get_product
from app.checkout import CheckoutRuleError, create_order_from_cart
from app.db import SessionLocal, init_db
from app.observability_logging import configure_logging, log_event, request_id_var
from app.replica import INJECTED_DELAY, REPLICA_ID
from grpc_service import checkout_pb2, checkout_pb2_grpc
from grpc_service.metrics import (
    GRPC_RPC_CALLS_TOTAL,
    GRPC_RPC_DURATION_SECONDS,
    GRPC_RPC_IN_FLIGHT,
)

logger = logging.getLogger("homeshop.grpc")


class RequestIdInterceptor(grpc.ServerInterceptor):
    """Put client/Envoy correlation metadata into the shared JSON log context."""

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler

        def correlated_behavior(request, context):
            supplied = next(
                (item.value for item in context.invocation_metadata() if item.key.lower() == "x-request-id"),
                None,
            )
            # Same rule as the HTTP middleware: accept only UUIDs, else generate one.
            try:
                request_id = str(uuid.UUID(supplied)) if supplied else str(uuid.uuid4())
            except ValueError:
                request_id = str(uuid.uuid4())
            # x-replica lets Envoy's access log record which replica answered.
            context.send_initial_metadata((("x-request-id", request_id), ("x-replica", REPLICA_ID)))
            token = request_id_var.set(request_id)
            try:
                return handler.unary_unary(request, context)
            finally:
                request_id_var.reset(token)

        return grpc.unary_unary_rpc_method_handler(
            correlated_behavior,
            request_deserializer=handler.request_deserializer,
            response_serializer=handler.response_serializer,
        )


def observed(method):
    """Record calls, status, duration and in-flight for one RPC method."""

    def decorator(fn):
        def wrapper(self, request, context):
            start = perf_counter()
            GRPC_RPC_IN_FLIGHT.labels(rpc_method=method).inc()
            status = grpc.StatusCode.UNKNOWN
            try:
                if INJECTED_DELAY["seconds"] > 0:
                    time.sleep(INJECTED_DELAY["seconds"])  # A3 Part D slow replica
                response = fn(self, request, context)
                status = grpc.StatusCode.OK
                return response
            except Exception:
                status = context.code() or grpc.StatusCode.INTERNAL
                raise
            finally:
                GRPC_RPC_CALLS_TOTAL.labels(rpc_method=method, status=status.name).inc()
                GRPC_RPC_DURATION_SECONDS.labels(rpc_method=method).observe(perf_counter() - start)
                GRPC_RPC_IN_FLIGHT.labels(rpc_method=method).dec()

        return wrapper

    return decorator


class CheckoutService(checkout_pb2_grpc.CheckoutServiceServicer):
    @observed("GetProduct")
    def GetProduct(self, request, context):
        if not request.product_id.strip():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "product_id is required")
        with SessionLocal() as db:
            try:
                product = get_product(db, request.product_id)
            except ProductNotFound:
                log_event(logger, logging.INFO, "grpc_request_complete", rpc_method="GetProduct", status="NOT_FOUND")
                context.abort(grpc.StatusCode.NOT_FOUND, "Product not found")
            log_event(logger, logging.INFO, "grpc_request_complete", rpc_method="GetProduct", status="OK")
            return checkout_pb2.ProductResponse(
                id=product.id,
                name=product.name,
                description=product.description or "",
                price_cents=product.price_cents,
                stock=product.stock,
                seller_id=product.seller_id,
            )

    @observed("Checkout")
    def Checkout(self, request, context):
        if not request.user_id.strip():
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "user_id is required")
        try:
            actor = validate_user_id(request.user_id)
        except ValueError as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))

        db = SessionLocal()
        try:
            order = create_order_from_cart(db, actor)
            created_at = order.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            result = checkout_pb2.CheckoutResponse(
                order_id=order.id,
                user_id=order.user_id,
                seller_id=order.seller_id,
                status=order.status.value,
                total_cents=order.total_cents,
                created_at=created_at.isoformat(),
                failure_reason=order.failure_reason or "",
            )
            log_event(
                logger,
                logging.INFO,
                "grpc_checkout_complete",
                rpc_method="Checkout",
                status="OK",
                order_id=order.id,
                total_cents=order.total_cents,
            )
            return result
        except CheckoutRuleError as error:
            db.rollback()
            log_event(
                logger,
                logging.WARNING,
                "grpc_checkout_rejected",
                rpc_method="Checkout",
                status=error.code,
            )
            code = (
                grpc.StatusCode.FAILED_PRECONDITION
                if error.code in ("empty_cart", "out_of_stock", "multiple_sellers")
                else grpc.StatusCode.INVALID_ARGUMENT
            )
            context.abort(code, error.message)
        except Exception as error:
            db.rollback()
            log_event(
                logger,
                logging.ERROR,
                "grpc_checkout_failed",
                rpc_method="Checkout",
                error_type=type(error).__name__,
            )
            context.abort(grpc.StatusCode.INTERNAL, "Checkout could not be completed")
        finally:
            db.close()


def start_grpc_server():
    """Start the gRPC server in background threads and return it."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32),
        interceptors=(RequestIdInterceptor(),),
    )
    checkout_pb2_grpc.add_CheckoutServiceServicer_to_server(CheckoutService(), server)
    # Standard grpc.health.v1 service, used by Envoy's gRPC active health check.
    health_servicer = health.HealthServicer()
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_servicer.set("homeshop.v1.CheckoutService", health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    port = int(os.getenv("GRPC_PORT", "50051"))
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    log_event(logger, logging.INFO, "grpc_server_started", port=port)
    return server


if __name__ == "__main__":
    configure_logging()
    init_db()
    start_grpc_server().wait_for_termination()
