FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12-slim-bookworm

COPY --from=uv /uv /uvx /bin/

WORKDIR /workspace
