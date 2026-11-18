FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt grpc-requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r grpc-requirements.txt

COPY app ./app
COPY grpc_service ./grpc_service
COPY scripts ./scripts

# One process per replica: HTTP on :8000 and gRPC on :50051 (started on app startup).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
