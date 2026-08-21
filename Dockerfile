# poc8 — manufacturing analytics POC
# Built and tested with: docker buildx build --platform linux/arm64 -t poc8:latest .
# (arm64 matches Graviton EC2 (t4g.*) and Apple Silicon Macs — no cross-compile penalty.
#  If you deploy on x86_64 (t3.*) instead, build with --platform linux/amd64.)

FROM python:3.11-slim

# --- OS deps -----------------------------------------------------------
# build-essential + curl needed by some duckdb/soda-core wheels at install time
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (cached layer) ----------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install DuckDB's vss extension (used by kb_manager.py for HNSW vector
# search) at build time instead of letting it download on first use at
# runtime. It's a real network fetch from extensions.duckdb.org — baking it
# into the image avoids a slow or failing first vector search, and means
# this feature doesn't depend on outbound internet access at runtime at all.
# NOTE: not verified end-to-end in this environment (this sandbox's own
# network egress list blocks extensions.duckdb.org) — confirm this layer
# builds cleanly in your actual Docker build, which has normal internet access.


# RUN python3 -c "import duckdb; duckdb.connect(':memory:').execute('INSTALL vss')"

# --- App code ------------------------------------------------------------
COPY . .

# Persistent data lives outside the image — mounted as a volume at runtime.
# This MUST be /app/storage, not an arbitrary name: app.py's STORAGE_DIR and
# logger_config.py's LOG_DIR both resolve relative to their own file's
# location, which is /app inside this image, giving /app/storage as the
# single root everything (DuckDB file, uploads, KB data, contracts, the
# rotating log file) actually gets written to. Mounting anywhere else is a
# silent no-op — the app just writes to the ephemeral container filesystem
# instead, and every restart or instance replacement wipes it.
RUN mkdir -p /app/storage
VOLUME ["/app/storage"]

EXPOSE 8000

# Simple container-level healthcheck; ALB target group does its own check too
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# IMPORTANT — DuckDB opens the on-disk file with a single-writer lock.
# Run exactly ONE process (--workers 1) and use threads for concurrency,
# not multiple worker processes, or you'll get "Conflicting lock" errors
# under concurrent requests. Adjust --threads based on load testing.
#
# --timeout 420 matches bedrock_client.py's botocore read_timeout (400s) —
# a single Bedrock call (schema discovery, YAML gen, NL-to-SQL) can
# legitimately take minutes with max_tokens=50000. gunicorn's default-ish
# 120s would kill the worker mid-request, dropping every in-flight user's
# request too, since this is a single process. The ALB's idle timeout
# (set in deploy.sh) is aligned to match — if either one is shorter than
# the other, that one wins and the client sees a broken/timed-out request
# even though the backend was still working.
#
# Entry point is main:app, NOT app:app — main.py has code that must run
# before anything else imports (see the top of main.py). Importing app:app
# directly would skip that entirely.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "420", "main:app"]