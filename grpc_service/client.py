"""Direct command-line client for HomeShop's Checkout RPC."""

import argparse
import os
import uuid

import grpc

from grpc_service import checkout_pb2, checkout_pb2_grpc


def main():
    parser = argparse.ArgumentParser(description="Call HomeShop checkout over gRPC")
    parser.add_argument("user_id", help="HomeShop demo identity, for example alice")
    parser.add_argument("--target", default=os.getenv("GRPC_TARGET", "localhost:50051"))
    parser.add_argument("--deadline", type=float, default=3.0, help="RPC deadline in seconds")
    parser.add_argument("--request-id", default=None, help="Optional UUID shared with a related HTTP request")
    args = parser.parse_args()

    request_id = args.request_id or str(uuid.uuid4())
    try:
        with grpc.insecure_channel(args.target) as channel:
            stub = checkout_pb2_grpc.CheckoutServiceStub(channel)
            response = stub.Checkout(
                checkout_pb2.CheckoutRequest(user_id=args.user_id),
                timeout=args.deadline,
                metadata=(("x-request-id", request_id),),
            )
        print(
            f"Checkout succeeded: order_id={response.order_id} "
            f"user_id={response.user_id} status={response.status} "
            f"total_cents={response.total_cents}"
        )
        print(f"request_id={request_id}")
    except grpc.RpcError as error:
        print(
            f"Checkout failed: code={error.code().name} details={error.details()} "
            f"request_id={request_id}"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
