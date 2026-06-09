# ==========================================
# Multi-stage Dockerfile for CERN Portfolio
# ==========================================

# Phase 1: Dependency builder
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Phase 2: Runtime runner image
FROM python:3.11-slim AS runner

WORKDIR /app

# Create a non-privileged group and user for security compliance
RUN groupadd -g 10001 cern-group && \
    useradd -u 10001 -g cern-group -m -s /bin/bash cern-op

# Copy installed dependencies from the builder phase
COPY --from=builder /root/.local /home/cern-op/.local
# Copy only the runtime artifacts (never the local venv or caches)
COPY --chown=cern-op:cern-group src/ /app/src/
COPY --chown=cern-op:cern-group mock_cern_confluence/ /app/mock_cern_confluence/

# Add pip binary path and pythonpath
ENV PATH=/home/cern-op/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Use the non-root user
USER cern-op

# Default action is running the offline metrics evaluation harness
CMD ["python", "-m", "src.eval_suite"]
