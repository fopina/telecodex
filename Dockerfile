# TODO: remove all file if no docker image is required (ie: just a python library)
FROM python:3.10-alpine AS base

# --- builder
FROM base AS builder
WORKDIR /app
RUN pip install uv
# TODO: replace with your package name
COPY example /src/example
COPY pyproject.toml README.md /src/
RUN uv pip install --target=/app /src

# --- main
FROM base
COPY --from=builder /app /app
ENV PYTHONPATH=/app

# TODO: replace with your package name
ENTRYPOINT ["python3", "-m", "example"]
