# ==========================================
# Multi-stage Dockerfile — MCP Confluence RAG
# Production target: Hugging Face Spaces (Docker, port 7860)
# ==========================================

FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-semantic.txt ./
RUN pip install --no-cache-dir --user torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --user -r requirements-semantic.txt

FROM python:3.11-slim AS runner

WORKDIR /app

RUN groupadd -g 10001 cern-group && \
    useradd -u 10001 -g cern-group -m -s /bin/bash cern-op

COPY --from=builder --chown=cern-op:cern-group /root/.local /home/cern-op/.local
COPY --chown=cern-op:cern-group src/ /app/src/
COPY --chown=cern-op:cern-group mock_cern_confluence/ /app/mock_cern_confluence/

RUN mkdir -p /app/.chroma && chown cern-op:cern-group /app/.chroma

ENV PATH=/home/cern-op/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/home/cern-op/.cache/huggingface

USER cern-op

RUN python -c "from llama_index.embeddings.huggingface import HuggingFaceEmbedding; \
    HuggingFaceEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')"

ENV MCP_TRANSPORT=streamable-http
ENV HTTP_PORT=7860
EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7860/health', timeout=4)"

CMD ["python", "-m", "src.server"]
