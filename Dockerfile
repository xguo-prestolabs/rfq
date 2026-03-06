FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libzmq5 \
    g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN uv sync --frozen

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
