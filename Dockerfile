# syntax=docker/dockerfile:1.6

FROM python:3.12-slim AS builder

WORKDIR /build
RUN pip install --no-cache-dir build

COPY pyproject.toml README.md ./
COPY softblue ./softblue
RUN python -m build --wheel --outdir /wheels


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SOFTBLUE_HOME=/data

RUN useradd --create-home --uid 1000 softblue \
    && mkdir -p /data \
    && chown softblue:softblue /data

COPY --from=builder /wheels/ /tmp/wheels/
RUN WHEEL=$(ls /tmp/wheels/softblue-*.whl) \
    && pip install --no-cache-dir "${WHEEL}[web]" \
    && rm -rf /tmp/wheels

USER softblue
WORKDIR /home/softblue

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=3).status==200 else 1)"

CMD ["softblue", "web", "--host", "0.0.0.0", "--port", "8080"]
