"""Separate gRPC transport for HomeShop checkout."""

import logging
import os
import uuid
from concurrent import futures
from datetime import timezone
from time import perf_counter

import grpc

from app.auth import validate_user_id
from app.checkout import CheckoutRuleError, create_order_from_cart
from app.db import SessionLocal, init_db
from app.observability_logging import configure_logging, log_event, request_id_var
from grpc_service import checkout_pb2, checkout_pb2_grpc
from grpc_service.metrics import (
    GRPC_RPC_CALLS_TOTAL,
    GRPC_RPC_DURATION_SECONDS,
    GRPC_RPC_IN_FLIGHT,
    start_metrics_server,
)

logger = logging.getLogger("homeshop.grpc")


class RequestIdInterceptor(grpc.ServerInterceptor):
    """Put client correlation metadata into the shared JSON log context."""

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
            context.send_initial_metadata((("x-request-id", request_id),))
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


class CheckoutService(checkout_pb2_grpc.CheckoutServiceServicer):
    def Checkout(self, request, context):
        method = "Checkout"
        start = perf_counter()
        GRPC_RPC_IN_FLIGHT.labels(rpc_method=method).inc()
        status = grpc.StatusCode.UNKNOWN
        try:
            response = self._checkout(request, context)
            status = grpc.StatusCode.OK
            return response
        except grpc.RpcError as error:
            status = error.code()
            raise
        except Exception:
            context_status = context.code()
            status = context_status if context_status is not None else grpc.StatusCode.INTERNAL
            raise
        finally:
            GRPC_RPC_CALLS_TOTAL.labels(
                rpc_method=method, status=status.name
            ).inc()
            GRPC_RPC_DURATION_SECONDS.labels(rpc_method=method).observe(
                perf_counter() - start
            )
            GRPC_RPC_IN_FLIGHT.labels(rpc_method=method).dec()

    def _checkout(self, request, context):
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


def serve():
    configure_logging()
    init_db()
    start_metrics_server(int(os.getenv("GRPC_METRICS_PORT", "8002")))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=(RequestIdInterceptor(),),
    )
    checkout_pb2_grpc.add_CheckoutServiceServicer_to_server(CheckoutService(), server)
    port = int(os.getenv("GRPC_PORT", "50051"))
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    log_event(
        logger,
        logging.INFO,
        "grpc_server_started",
        port=port,
        metrics_port=int(os.getenv("GRPC_METRICS_PORT", "8002")),
    )
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
