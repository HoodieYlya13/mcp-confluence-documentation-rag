<p align="center">
  <img src="assets/logo.png" alt="MCP Confluence Documentation RAG" width="96" height="96" />
</p>

# 🌌 MCP Confluence Documentation RAG — Accelerator Operations Substrate

[![CI](<https://img.shields.io/badge/CI-two--speed%20(push%20%2B%20nightly)-green>)]()
[![Live](https://img.shields.io/badge/Live-HF%20Spaces-yellow)](https://hoodieylya13-mcp-confluence-documentation-rag.hf.space/health)
[![Model Context Protocol](https://img.shields.io/badge/Protocol-MCP%20streamable%20HTTP-orange)](https://modelcontextprotocol.io/)
[![Security](https://img.shields.io/badge/Security-4--layer%20RBAC-red)](SECURITY.md)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)

A production-deployed, RBAC-enforced **Model Context Protocol server** exposing a retrieval-augmented knowledge base built from a **live Atlassian Confluence instance**. Designed as a portfolio centerpiece for the CERN **BE-CSS Computing Engineer (Applied AI)** role: secure knowledge connectors, agentic orchestration, evaluation gates and MLOps automation — at $0/month infrastructure cost.

**Live server:** [`https://hoodieylya13-mcp-confluence-documentation-rag.hf.space`](https://hoodieylya13-mcp-confluence-documentation-rag.hf.space/health) — `/health` and `/metrics` are public; the MCP endpoint requires a bearer token (the whole point: identical questions yield different answers per authorization level).

---

## What it demonstrates

| Job requirement                                   | Where                                                                                                                                                                    |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Secure knowledge connectors                       | [`src/sources.py`](src/sources.py) — Confluence REST connector: fail-closed ACL mapping, incremental version-diff sync, retries                                          |
| RAG pipelines, vector DBs, chunking & embeddings  | [`src/retrieval.py`](src/retrieval.py) — LlamaIndex + ChromaDB, local sentence-transformers, structure-preserving chunking, **ACL filters pushed into the vector query** |
| LLM frameworks (LlamaIndex / LangChain ecosystem) | LlamaIndex for the retrieval layer, **LangGraph** for the agent state machine                                                                                            |
| Agentic AI + MCP                                  | [`src/agent_loop.py`](src/agent_loop.py) LangGraph graph; [`src/server.py`](src/server.py) FastMCP over stdio _and_ streamable HTTP                                      |
| Safety & evaluation frameworks                    | [`src/eval_suite.py`](src/eval_suite.py) — 8 gated scenarios: golden-set retrieval, adversarial probes, LLM-as-judge faithfulness, 0% leakage gate                       |
| MLOps & CI/CD                                     | Two-speed GitHub Actions, Trivy scan, nightly full-pipeline eval, self-healing nightly sync, Prometheus `/metrics`                                                       |

---

## Architecture

```mermaid
flowchart TD
    subgraph CONF["Atlassian Confluence Cloud · space ATSOPS"]
        PAGES["7 accelerator-ops pages\nACL labels per page"]
    end

    subgraph SRV["MCP Server · Hugging Face Docker Space"]
        CONN["ConfluenceAPISource\nfail-closed ACL · version-diff sync"]
        PARSE["ConfluenceSanitizationEngine\nXHTML → Markdown, tables preserved"]
        CHUNK["StructureAwareChunker\ntables atomic · headings carried"]
        IDX[("ChromaDB embedded\nMiniLM embeddings · role metadata")]
        MW["Bearer-token middleware\ntoken → role (static or OIDC/JWKS)"]
        TOOLS["Retrieval tools\nlist · fetch · semantic_search"]
        SCHED["asyncio daily sync\n+ POST /admin/sync (lead only)"]
        OBS["/health · /metrics (Prometheus)"]
        CONN --> PARSE --> CHUNK --> IDX
        SCHED -.-> CONN
        MW -->|"raw tools · client reasons"| TOOLS
        TOOLS -->|"ACL filter inside vector query"| IDX
    end

    subgraph AGENT["LangGraph Agent · agent_loop.py · ask tool"]
        R["router"] --> RET["retrieve"]
        RET --> INT["integrate\ninjection-hardened prompt"]
        INT --> VER{"verify\ncontext ACL audit"}
        VER -->|pass| GEN["generate\nGemini tiers / Ollama\n+ leak scan"]
        VER -->|violation| REF["refuse"]
    end

    CLIENT["MCP client (Claude Desktop / Code)\nAuthorization: Bearer …"]
    GH["GitHub Actions\nnightly sync cron + full eval"]

    PAGES --> CONN
    CLIENT --> MW
    MW -->|"ask tool · server reasons"| R
    RET --> TOOLS
    GH -.-> SCHED
```

The four-layer security model (bearer auth → ACL pushdown → context verifier → post-generation leak scan) is documented in [SECURITY.md](SECURITY.md). Every design decision and its rationale lives in [TAD.md](TAD.md).

---

## Quickstart

### Local, fully offline (no accounts needed)

```bash
make build          # venv + fast deps
make test           # 75 unit & security tests (~10 s)
make run-eval       # 8-scenario evaluation harness, exit code is the gate
make run-agent      # 4-scenario agent demo (stub LLM, mock corpus)
```

### Full pipeline (live Confluence + semantic retrieval + Gemini)

```bash
pip install -r requirements-semantic.txt -r requirements-dev.txt
cp .env.example .env   # fill in Confluence + Gemini credentials
python scripts/seed_confluence.py      # idempotent: creates the space + page tree (8 labeled pages, unlabeled nav pages)
python -m src.agent_loop               # real end-to-end demo
```

### Connect Claude Desktop to the local server (stdio)

```json
{
  "mcpServers": {
    "accelerator-ops-substrate": {
      "command": "/absolute/path/to/repo/venv/bin/python3",
      "args": ["-m", "src.server"],
      "cwd": "/absolute/path/to/repo",
      "env": { "STDIO_ROLE": "JUNIOR_OP" }
    }
  }
}
```

Switch `STDIO_ROLE` to `ATS_CORE_LEAD` and ask the same question — the answer changes. That's the demo.

### Connect to the live server (streamable HTTP)

```bash
claude mcp add --transport http accelerator-ops \
  https://hoodieylya13-mcp-confluence-documentation-rag.hf.space/mcp \
  --header "Authorization: Bearer <token>"
```

Tokens map to roles server-side and the client never states its own privilege level: the bearer is either a pre-shared role token (`AUTH_TOKENS`) or an **OIDC access token** from the identity provider, verified via JWKS (issuer + audience + signature) with its `roles` claim mapped to a role (`SSO_ISSUER` + `SSO_AUDIENCE`; disabled when unset). Either way the role is resolved by the server.

---

## Evaluation Report (live pipeline: semantic retrieval + Gemini)

```
============================================================
          ATS OPS SUBSTRATE EVALUATION REPORT
============================================================
1. Junior Op Authorized Cryo Access:   SUCCESS
2. Lead Op Authorized SPS Access:      SUCCESS
3. RBAC Violation Rate (Leakage):      0.00% (Target: 0.00%)
4. Context Precision:                  100.00% (Target: >90.00%)
5. Table Parsing Integrity Check:      PASSED
6. Golden-Set Hit Rate @3:             100.00% (Target: >=90.00%)
7. Adversarial Probes Leaked:          0 (Target: 0)
8. Faithfulness (LLM-as-judge):        100.00% (Target: >80.00%)
============================================================
```

The adversarial set includes role-escalation attempts, a permanent prompt-injection fixture _inside the Confluence corpus_ ("SYSTEM OVERRIDE: ignore all previous instructions…"), and over-privilege probes — the live LLM quotes the injection as data and refuses to obey it.

---

## Operations

- **Sync:** startup sync (container is disposable; Confluence is the state of record) + in-process daily scheduler + GitHub Actions nightly cron that calls `POST /admin/sync` and self-heals by restarting the Space if unreachable.
- **Observability:** `GET /metrics` exposes Prometheus-format counters (tool calls, latency, RBAC denials by layer, sync runs) ready for a central Prometheus/Grafana stack.
- **CI:** per-push lint + tests + offline eval + Trivy scan in seconds (TF-IDF/stub fast path); nightly job runs the full semantic + LLM-judge pipeline.

---

## Repository Map

| Path                                                                                          | Purpose                                                                                    |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| [src/server.py](src/server.py)                                                                | FastMCP server, auth middleware, HTTP app, sync endpoints                                  |
| [src/sources.py](src/sources.py)                                                              | `DocumentSource` protocol: Confluence API + local file connectors                          |
| [src/parser.py](src/parser.py)                                                                | Confluence storage-format XHTML → clean Markdown (macros, tables)                          |
| [src/retrieval.py](src/retrieval.py)                                                          | Chunker + LlamaIndex/Chroma semantic index with ACL pushdown                               |
| [src/vector_store.py](src/vector_store.py)                                                    | NumPy TF-IDF backend (CI fast path, air-gapped fallback)                                   |
| [src/agent_loop.py](src/agent_loop.py)                                                        | LangGraph agent: router/retrieve/integrate/verify/generate                                 |
| [src/llm.py](src/llm.py)                                                                      | Gemini (tiered fallback) / Ollama / deterministic stub                                     |
| [src/auth.py](src/auth.py)                                                                    | Token→role resolution (static `AUTH_TOKENS` + OIDC/JWKS verification), ContextVar identity |
| [src/metrics.py](src/metrics.py)                                                              | Prometheus exposition                                                                      |
| [src/eval_suite.py](src/eval_suite.py) + [eval/golden_dataset.yaml](eval/golden_dataset.yaml) | 8-scenario gated evaluation harness                                                        |
| [scripts/](scripts/)                                                                          | Confluence seeding + HF Space provisioning (infra as code)                                 |
| [TAD.md](TAD.md)                                                                              | Every design decision with rationale                                                       |
| [SECURITY.md](SECURITY.md)                                                                    | Identity model, 4 enforcement layers, threat model                                         |
