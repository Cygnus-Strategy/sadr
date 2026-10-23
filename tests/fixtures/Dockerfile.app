# syntax=docker/dockerfile:1
ARG BASE=python:3.15-slim
FROM python:3.15-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/
RUN npx --yes wrangler@4 deploy
FROM build AS test
FROM ${BASE}
COPY --from=build /app /app
FROM python:3.15-slim@sha256:f48fda6d59b2b3437781d1c3ccf4aa0e26ae871de3cdd758b49ab45ee7291619
FROM scratch
