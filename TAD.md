# Technical Architecture Document
## MCP Confluence Documentation RAG — CERN ATS Portfolio Project

---

## 1. Project Scope

A zero-external-dependency, offline-first MCP server implementing a secure RAG pipeline over Atlassian Confluence XHTML exports. Designed as a CERN BE-CSS portfolio piece, the primary constraint is that it must demonstrate production-grade security thinking (double-layer RBAC, adversarial injection resistance) while running without any cloud API dependency — satisfying the network isolation policies relevant to safety-critical environments like the CCC.

The project is a proof-of-concept. The mock data layer (`mock_cern_confluence/`) and the hardcoded response routes in `agent_loop.py` are intentional stand-ins for a real Confluence API + LLM; the architecture is designed so both can be swapped at clearly defined extension points without touching the security or retrieval layers.

---

## 2. Data & Execution Flow

```
mock_cern_confluence/*.html
    │
    ▼ ConfluenceSanitizationEngine.parse_file()         [parser.py]
ParsedDocument (doc_id, space, allowed_roles, clean_content)
    │
    ▼ LocalVectorIndex.add_documents()                  [vector_store.py]
DocumentChunk[] + TF-IDF matrix (NumPy)
    │
    ▼ initialize_substrate()  [called at import time]   [server.py]
DOCUMENTS dict + INDEX singleton
    │
    ├─ list_available_pages(user_role)
    ├─ fetch_and_sanitize_page(page_id, user_role)
    └─ semantic_search_accelerator(query, user_role, top_k)
              │
              ▼ called by Phase 1 Router
    OperationalAgentSubstrate.run_turn()               [agent_loop.py]
              │
    Phase 1: Route query → MCP tool call
    Phase 2: Weave chunks into system prompt
    Phase 3: Verify every chunk against user_role → raise or pass
              │
              ▼
    Response string (or security refusal)
```

---

## 3. Module Design Decisions

### 3.1 `config.py`

**Structured JSON logging to stderr.**
MCP servers communicate with clients over stdio. The protocol channel is stdout; any text written there is interpreted as an MCP message and corrupts the session. All log output is therefore routed to stderr, which the MCP host process (e.g. Claude Desktop) captures separately. The formatter produces single-line JSON for compatibility with Kibana/Elasticsearch log pipelines typically used at CERN.

**ISO-8601 UTC timestamps.**
`datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()` rather than the default `asctime` string. Timezone-aware ISO-8601 is unambiguous, sortable, and parseable without locale configuration — required for log aggregators.

**`standard_fields` filter set.**
The `StructuredJsonFormatter` merges `record.__dict__` extras into the JSON payload. The filter set prevents Python's own `LogRecord` internals from polluting the output. `taskName` is explicitly included in the filter because Python 3.12 added it to `LogRecord`; without it, every log line in 3.12+ carries `"taskName": null`.

**`KNOWN_ROLES` frozenset.**
Acts as a closed security boundary. Every MCP tool validates its `user_role` argument against this set before touching any data. Using a `frozenset` makes membership checks O(1) and prevents accidental mutation.

**Handler deduplication in `configure_logging()`.**
The function is called at module import time from both `server.py` and `agent_loop.py`. Without the guard, each import would attach another `StreamHandler`, duplicating every log line.

---

### 3.2 `parser.py`

**Metadata in HTML comment blocks.**
ACL and document identity (`doc_id`, `space`, `allowed_roles`, `last_modified`) are stored in a JSON object inside an HTML comment at the top of each file: `<!-- { "doc_id": "...", ... } -->`. This mirrors how a real Confluence-to-file export pipeline would embed page-level metadata (sourced from `rest/api/content?expand=restrictions`) alongside the storage-format XHTML — keeping metadata co-located with content rather than in a separate sidecar file.

**Iterative while-loop for macro sanitization.**
`_sanitize_macros()` uses `while True: soup.find(macro_name)` rather than `soup.find_all()` + loop. Real Confluence exports routinely nest macros inside macros (e.g. a warning box inside an expand block). A single-pass `find_all()` iteration leaves inner macros intact because they were part of the body of an outer macro that was replaced. The iterative `find()` approach always operates on the current state of the DOM, so nested macros are peeled off one level at a time until none remain.

**Residual `ac:*` / `ri:*` tag sweep.**
After the structured-macro loop, a final `find_all(re.compile(r"^(ac|ri):"))` + `unwrap()` pass cleans up any remaining Atlassian-namespaced tags (attachment references, layout containers, image tags) that are not `ac:structured-macro` and therefore skipped by the main loop.

**Table pre-conversion before markdownify.**
`_sanitize_tables()` replaces `<table>` elements with pre-rendered Markdown table strings before passing the DOM to `markdownify`. If tables were left for markdownify to convert, its output would be inconsistent for complex merged-cell or nested structures. Pre-converting gives full control over column alignment and cell normalization.

**markdownify escaping disabled at the call site.**
`escape_asterisks=False, escape_underscores=False, escape_misc=False` prevents markdownify from backslash-escaping technical identifiers like `VGPB_34_Q1`, `1.2e-5`, or `H-` ions. These identifiers must survive verbatim into the chunks so the TF-IDF tokenizer indexes them correctly. This replaced a fragile post-processing `_unescape_markdown()` regex pass that was doing the inverse transformation after the fact.

**Markdown table cell normalization.**
Inner newlines in cells are collapsed to spaces; column count is padded or truncated to match the header width. This prevents malformed Markdown tables (ragged column counts break most renderers and confuse the table dimension integrity check in the eval suite).

**Blank-line wrapping of Markdown tables.**
`_convert_table_to_markdown()` wraps its output in `\n\n...\n\n`. This ensures that when the text is later split into chunks, a table block is never partially stranded at the boundary of two chunks without leading/trailing context.

**Post-processing: collapse blank lines.**
`re.sub(r"\n{3,}", "\n\n", ...)` collapses runs of 3+ blank lines to exactly 2. markdownify emits extra blank lines around block elements; leaving them in wastes token budget when the text is chunked.

---

### 3.3 `vector_store.py`

**Constructor-time chunk parameter validation.**
`chunk_size` and `chunk_overlap` are validated in `__init__` rather than at first use. This fails fast with a clear error before any documents are loaded, rather than producing silent or confusing downstream failures.

**Sliding window stride guaranteed positive.**
The stride is `chunk_size - chunk_overlap`. The constructor enforces `0 <= overlap < chunk_size`, which guarantees stride ≥ 1 and eliminates the possibility of an infinite loop in `chunk_document()`.

**Defensive copy of `allowed_roles` per chunk.**
`allowed_roles=list(doc.allowed_roles)` creates an independent list per chunk. Without this, every chunk from the same document would reference the same list object; mutating one would silently mutate all of them. This is particularly important for the adversarial injection test in `agent_loop.py` where a synthetic chunk is appended with different ACL values.

**Tokenizer regex: `\b[a-zA-Z0-9_]{2,}\b`.**
Includes underscores so compound identifiers like `VGPB_34_Q1` and `VME_SPS` are tokenized as single tokens. The minimum length of 2 filters out single-character noise that would inflate the vocabulary without adding retrieval signal.

**Vocabulary sorted for determinism.**
`sorted(list(unique_tokens))` produces the same vocabulary regardless of Python's set iteration order. This makes the TF-IDF matrix and cosine scores reproducible across runs, which is required for the eval suite assertions to be stable.

**Smooth IDF formulation: `log((1 + N) / (1 + df)) + 1`.**
The `+1` additions prevent division by zero when `df == 0` (a token appears in zero chunks, which cannot happen for vocab tokens but is protected against anyway), and the `+ 1` additive offset ensures all IDF values are positive — tokens appearing in every chunk get IDF = 1 rather than 0, so they still contribute to similarity rather than being zeroed out.

**Zero-similarity early exit.**
The sorted indices are iterated in descending score order. When `score <= 0.0` is hit, the loop breaks: all remaining chunks share no vocabulary with the query and would inject noise into the agent's context. This also avoids O(N) iteration over the full chunk list for every search.

**RBAC applied during sort iteration, not as a pre-filter.**
Filtering happens inline while collecting `top_k` results, not as a separate `filter()` pass before sorting. This means RBAC-filtered chunks still consume "slots" in the sorted list, preserving the guarantee that the returned results are the top-scoring *authorized* chunks — not the top-scoring chunks of all authorized documents regardless of score.

---

### 3.4 `server.py`

**Substrate initialized at module import time.**
`initialize_substrate()` is called at the module level. When the MCP server starts (via `mcp.run()`), the index is already built and ready; there is no cold-start latency on the first tool call. The same initialization runs when the module is imported by the test suite, ensuring tests operate on the same index state as the running server.

**`_validate_role()` at every tool boundary.**
All three MCP tools call `_validate_role(user_role)` before accessing `DOCUMENTS` or `INDEX`. This is the outermost security boundary: any role string not in `KNOWN_ROLES` is rejected before any data retrieval path is entered. The RBAC inside `LocalVectorIndex.similarity_search()` is the second, inner boundary (defense-in-depth).

**`top_k` clamped to [1, 10].**
A misbehaving or adversarial MCP client could pass `top_k=10000` to attempt a full index dump. Clamping at the server boundary before passing to the index prevents this without requiring the vector store to know about MCP-level concerns.

**`user_role` accepted from tool input (PoC only).**
In this proof-of-concept the role is supplied by the MCP client. In a production deployment at CERN this must be derived server-side from an authenticated identity (e.g. CERN SSO / OIDC claims) and the `user_role` parameter removed from the tool signatures entirely. The two-layer RBAC enforcement (retrieval + generation) is deliberately decoupled so swapping the identity provider requires no changes to the retrieval substrate.

**Global `DOCUMENTS` + `INDEX` module-level singletons.**
FastMCP tool functions are stateless callables; shared state must live at module scope. A dependency-injection pattern would add abstraction without benefit at this scale.

---

### 3.5 `agent_loop.py`

**Three-phase pipeline design.**
Phase 1 (Router) decides whether retrieval is needed and calls the MCP tool. Phase 2 (Context Integrator) assembles the system prompt. Phase 3 (Verifier) independently re-checks that every retrieved chunk's `allowed_roles` contains the active `user_role` before the response is generated. This mirrors the LangGraph pattern of composable graph nodes with explicit state handoff between phases.

**Phase 3 as a second, independent RBAC check.**
The vector store's RBAC filtering (Phase 1 / retrieval layer) is the first line of defense. The Verifier (Phase 3) is a separate, decoupled audit that runs on the chunks *after* they have been assembled into the context. If the retrieval layer ever malfunctions (e.g. a bug in `similarity_search()` allows a restricted chunk through), Phase 3 catches it. Neither layer trusts the other. The two are implemented in different modules with no shared code path.

**Whole-token greeting detection.**
`_is_greeting()` tokenizes the query with `re.findall(r"[a-z']+", ...)` and checks membership in `GREETING_TOKENS`. Substring matching (`"hi" in query`) would false-positive on technical queries like "BPM hits high" because `"hi"` is a substring of `"hits"` and `"high"`. The 4-token length cap prevents a long technical query that happens to start with "hello" from being misrouted.

**`RESPONSE_ROUTES` data-driven routing table.**
The mock LLM response logic was refactored from a copy-paste if-else chain into a list of route dictionaries. Each route specifies: topic keywords (for query matching), expected `doc_id` (to verify the chunk actually came from retrieval, not a hallucination), `detail_triggers` (substrings indicating high-precision retrieval), a detailed response, and a summary fallback. This structure makes the mock generation layer easy to extend without touching control flow, and kept individual lines under the 110-char ruff limit.

**`NO_CONTEXT_RESPONSE` constant.**
The "no relevant documents found" response appears in two places: when retrieval returns no chunks, and when the routing table finds a matching topic keyword but the corresponding doc's chunks were removed by RBAC. Extracting it as a constant ensures the two paths return identical text, which is important for the eval suite's string-match assertions.

**`force_inject_leak` parameter.**
A boolean flag on `run_turn()` that artificially injects a restricted chunk into the context before Phase 3 runs. This is the mechanism used by the eval suite and tests to verify that the Verifier correctly aborts on a contaminated context, without needing to actually break the retriever.

---

### 3.6 `eval_suite.py`

**Five-scenario test battery.**
The eval suite is a separate metrics harness distinct from the pytest suite. It measures operational correctness: can an authorized user get the right answer, can an unauthorized user get nothing, does context precision hold, are tables parsed correctly? pytest covers unit correctness (individual functions); the eval suite covers end-to-end system behavior.

**Four adversarial leakage vectors in Scenario 2.**
Scenario 2 tests four separate attack surfaces against a JUNIOR_OP: (1) the agent turn (tests the full pipeline), (2) direct `fetch_and_sanitize_page` tool call (tests page-level RBAC), (3) direct `semantic_search_accelerator` call (tests chunk-level RBAC), (4) `list_available_pages` (tests metadata visibility). All four must be blocked for the leakage-free assertion to pass.

**Dynamic table dimension comparison.**
`evaluate_parsing_integrity()` reads the raw HTML source file for each document, counts rows and columns in every `<table>` element, and compares against the Markdown tables in the sanitized output. The original implementation hardcoded `expected_rows=5, expected_columns=5` for a single document — fragile and wrong for multi-document or multi-table pages. The dynamic approach is self-updating when new mock documents are added.

**Markdown divider row offset (`len(rows) + 1`).**
A Markdown table has one more row than its HTML source: the `| --- | --- |` separator row that Markdown requires between the header and data rows. The `+1` accounts for this when comparing expected vs actual row counts.

**`sys.exit()` based on `overall_status`.**
The eval suite previously always exited with code 0. CI pipelines interpret exit code 0 as success; a silent FAIL would never block a merge. `sys.exit(0 if ... == "PASS" else 1)` makes the eval harness a proper CI gate.

**Context Precision formula.**
`relevant_count / total_retrieved` where "relevant" means the chunk came from the document topically matching the query (`lhc_cryo_troubleshooting` for a cryo pressure query). This is a simplified offline precision metric that does not require human relevance labels — usable in fully automated CI.

---

## 4. Security Architecture

### Double-Layer RBAC

Two independent, decoupled RBAC checks protect against different failure modes:

| Layer | Location | Checks | Fails if |
|---|---|---|---|
| Layer 1 (Retrieval) | `LocalVectorIndex.similarity_search()` | `user_role in chunk.allowed_roles` per chunk | Retriever bug allows restricted chunk through |
| Layer 2 (Generation) | `OperationalAgentSubstrate._verify_rbac_guardrails()` | Same check on assembled context | Any chunk in context is unauthorized |

Layer 2 exists because Layer 1 is inside a library that could theoretically have a bug. The Verifier is a last-resort safety net at the response generation boundary.

### Role Validation at the MCP Boundary

`_validate_role()` in `server.py` rejects role strings not in `KNOWN_ROLES` before any retrieval code runs. This prevents:
- Typos in client code silently defaulting to an unexpected access level
- Adversarial clients probing with invented role strings to find gaps

### ACL List Isolation

Each `DocumentChunk` holds a defensive copy (`list(doc.allowed_roles)`) of the parent document's ACL. This prevents a mutation of one chunk's ACL from cascading to all chunks of the same document, which would be a silent privilege escalation bug.

---

## 5. Infrastructure & Tooling

### Dockerfile (Multi-Stage)

Two stages: `builder` installs Python dependencies into `/root/.local` (pip's `--user` install path); `runner` copies only `/root/.local` and the runtime source (`src/`, `mock_cern_confluence/`). The local `venv/` directory is never copied into the image. The runner uses a non-root user (`cern-op`, UID 10001) for compliance with CERN's container security policies and Kubernetes `runAsNonRoot` admission controls. `PYTHONUNBUFFERED=1` ensures stdout/stderr are not buffered inside the container, which is critical for MCP stdio transport.

### `pyproject.toml` — pytest configuration

`pythonpath = ["."]` adds the repo root to `sys.path` for every pytest run. This allows `from src.X import Y` imports in tests without requiring `PYTHONPATH=.` to be set manually or prepended to every command. `--strict-markers` prevents tests from accidentally using undefined markers without failing loudly.

### `pyproject.toml` — ruff configuration

`line-length = 110` (not 88): the default 88 is too narrow for the structured log calls and MCP tool docstrings that carry descriptive argument text. `target-version = "py310"` locks syntax checks to the minimum supported version. `UP006`/`UP035` are ignored to keep `typing.List` / `typing.Dict` for broad Python version readability rather than enforcing the `list[...]` syntax that requires 3.9+.

### GitHub Actions CI

`fail-fast: false` on the Python 3.11/3.12 matrix: both versions must fully report their results even if one fails, so version-specific regressions are visible at a glance. The three-stage pipeline (ruff → pytest → eval harness) is ordered from fastest to slowest; ruff exits in under a second and blocks the expensive stages on style violations.

---

## 6. Extension Points (PoC era — all delivered in v2)

Sections 1–5 describe the original offline proof-of-concept. Every extension point listed in earlier revisions of this document has since been implemented; Part II below documents the production system and its decisions. The PoC components (mock files, TF-IDF store, stub LLM) survive as the deterministic fast path used by per-push CI.

---

# Part II — Production Architecture (v2)

## 7. Production Data & Execution Flow

```
Atlassian Confluence Cloud (space ATSOPS, ACL labels per page)
    │
    ▼ ConfluenceAPISource.fetch_documents()             [sources.py]
      pagination · retries/backoff · fail-closed ACL · version-diff cache
ParsedDocument[]  (doc_id = Confluence page id, title in metadata)
    │
    ▼ SemanticVectorIndex.add_documents()               [retrieval.py]
      StructureAwareChunker → TextNodes → all-MiniLM-L6-v2 → ChromaDB
    │
    ▼ initialize_substrate()  [startup + daily scheduler + /admin/sync]
    │
    ├─ MCP tools (identity from bearer token, never from client input)
    │     list_available_pages() · fetch_and_sanitize_page(page_id)
    │     semantic_search_accelerator(query, top_k)
    │
    ▼ LangGraph StateGraph                              [agent_loop.py]
      router → retrieve → integrate → verify ⇒ generate | refuse
                                        │
                                        ▼ Gemini (tiered) / Ollama / stub
                              post-generation leak scan (Layer 3)
```

Live deployment: Hugging Face Docker Space, MCP streamable HTTP at `/mcp`, public `/health` and `/metrics`, lead-only `POST /admin/sync`.

## 8. Configuration (`settings.py`)

**pydantic-settings with `.env` file + environment override.** All knobs (source, retriever backend, LLM backend, transport, tokens) resolve through one `Settings` class. Real env vars take precedence over `.env`, which is how `tests/conftest.py` pins the hermetic test profile (`local` source, `tfidf` retriever, `stub` LLM, empty `STDIO_ROLE`) regardless of the developer's live `.env`. `get_settings()` is `lru_cache`d: one consistent snapshot per process.

## 9. Secure Confluence Connector (`sources.py`)

**`DocumentSource` protocol** with `ConfluenceAPISource` (production) and `LocalFileSource` (CI / air-gapped). The server chooses by config; nothing downstream knows the difference.

**ACL via page labels, not page restrictions.** Confluence Cloud Free does not support page restrictions (Standard-plan feature). ACLs are therefore encoded as labels (`acl-junior-op`, `acl-ats-core-lead`) and mapped to roles through a config dictionary. The production swap at CERN replaces the label reader with `expand=restrictions.read.restrictions` + SSO group mapping — the adapter seam is identical. The label→role map lives in `Settings`, not code.

**Fail closed.** A page with no recognized ACL label is skipped and logged as a security event — never indexed. This was first validated in production by accident: Confluence auto-creates a space homepage with no labels, and the connector correctly excluded it. The seeded space now leans into that behavior — the homepage and the section index pages are deliberately left unlabeled, so every live sync report shows the gate working (`pages_skipped_no_acl: 4`).

**Incremental sync.** The connector caches `(version.number, ParsedDocument)` per page id and re-parses only pages whose version changed. The live sync report (`pages_checked / changed / unchanged / skipped_no_acl / parse_errors`) is structured-logged and returned by `/admin/sync`.

**Resilience.** Exponential-backoff retries (3 attempts) on transport errors and 5xx; 4xx fails immediately (auth/config problems are not transient). If the source is unreachable at sync time, the server keeps serving the last-known-good index.

## 10. Semantic Retrieval (`retrieval.py`)

**Framework strategy: LlamaIndex for plumbing, custom for security.** LlamaIndex provides `TextNode`, `VectorStoreIndex`, `HuggingFaceEmbedding` and the Chroma binding; the chunker and the ACL model are custom because no framework ships layered RBAC.

**Structure-Preserving Layout Chunking.** The chunker splits Markdown into heading / table / paragraph blocks, packs blocks into chunks under a word budget, and enforces two invariants: a table block is never split (scientific tables lose meaning when cut), and the active heading is prepended to every chunk it governs (a continuation chunk still knows it belongs to "## Vacuum Sensor Thresholds"). Each chunk carries a defensive copy of the document ACL.

**ChromaDB embedded, ACL pushed into the query.** Role authorization is stored as integer metadata flags (`role_junior_op: 1`) and enforced with a LlamaIndex `MetadataFilter` inside the vector query — filter-at-source rather than post-filtering. Integers, not booleans: `MetadataFilter` validates `value` as `int | float | str | list`, rejecting `bool`. A belt-and-braces post-check re-verifies every returned chunk and logs CRITICAL if the pushdown ever fails.

**Local embeddings (`all-MiniLM-L6-v2`).** 80 MB, CPU-friendly, no embedding API: $0 and the air-gapped story stays intact. The model is baked into the Docker image at build time so cold starts never download it.

**Re-sync semantics.** `add_documents` deletes all chunks of current and stale doc ids, then inserts fresh nodes — idempotent upsert plus garbage collection of pages deleted in Confluence.

**TF-IDF retained.** The NumPy store remains behind the same interface as the CI-fast backend: zero model download, deterministic scores, sub-second test suite.

## 11. LLM Layer (`llm.py`)

**`LLMClient` protocol, three implementations.** `GeminiClient` (default, free tier) walks a configurable model-tier list and falls through on quota/error — the same pattern proven in the YlyaBot project. `OllamaClient` is the air-gapped switch (one env var, same pipeline). `StubLLMClient` deterministically echoes the retrieved context block, which keeps the eval harness's content assertions meaningful without any API.

**Context markers live here.** `CONTEXT_BEGIN/END` are defined in `llm.py` and imported by the agent — the stub and the prompt template can never drift apart.

## 12. LangGraph Agent (`agent_loop.py`)

**Explicit `StateGraph`:** `router → {greet | retrieve} → integrate → verify → {generate | refuse}`. The Verifier is a named node with a conditional edge, not buried logic; `export_graph_mermaid()` renders the safety pipeline for documentation.

**Prompt-injection hardening.** Retrieved content is wrapped in `<<<CONTEXT>>>` delimiters and the system prompt declares it untrusted data whose instructions must never be followed. A seeded Confluence page permanently contains a "SYSTEM OVERRIDE" injection string as a regression fixture; live Gemini quotes it as data and does not obey it, even when the user explicitly asks it to follow embedded instructions.

**Layer 3 — post-generation leak scan.** Any hex register token (`0x…`) in the generated answer that does not appear in the authorized context blocks the response. Catches both leakage and hallucinated register addresses.

**`AGENT_RETRIEVAL_TOP_K = 5`.** With 31 chunks across 7 documents, top-3 left the threshold *table* chunk at rank 4 behind three prose chunks (number-dense tables embed worse against natural-language questions than prose does); Gemini then truthfully answered "not in context". Retrieval depth 5 closes that gap at negligible cost.

## 13. Identity & Auth (`auth.py`, middleware in `server.py`)

**`user_role` is gone from every tool signature** — closing the documented weakness of the PoC. Identity is resolved server-side:

- **HTTP:** `Authorization: Bearer <token>` → ASGI middleware → token→role registry (`AUTH_TOKENS` secret) → `ContextVar` scoped to the request. Mirrors an upstream OIDC claim flow: at CERN the same seam consumes CERN SSO (Keycloak) tokens and the registry lookup becomes a group-claim mapping.
- **stdio:** the launching environment supplies `STDIO_ROLE` — local process identity, suited to a desktop client owned by one user.
- A token mapping to an unknown role fails closed (treated as invalid, logged as a security event).

**`ContextVar`, not a global.** Correct per-request isolation under asyncio concurrency; `role_context()` is also the test/agent seam for impersonating personas in-process.

**DNS-rebinding protection disabled deliberately.** The MCP SDK's streamable-HTTP transport rejects non-localhost `Host` headers by default (HTTP 421), a protection aimed at *unauthenticated localhost dev servers*. This deployment is the opposite case: a public hostname behind Hugging Face's reverse proxy with its own bearer-token layer in front of every MCP route.

## 14. Sync Automation

Three cooperating triggers, all calling the same `initialize_substrate()`:

1. **Startup sync** — mandatory anyway because HF free-tier disk is ephemeral; the container is disposable and Confluence is the state of record. Index rebuild is seconds at this scale.
2. **In-process scheduler** — an asyncio task (spliced into the Starlette lifespan around the MCP session manager) re-syncs every `SYNC_INTERVAL_HOURS` (24h default). Zero infrastructure.
3. **GitHub Actions nightly cron** — the external safety net: calls `POST /admin/sync` (requires the ATS_CORE_LEAD token); if unreachable, restarts the Space via the HF API and polls `/health`. Self-healing visible in the public Actions history.

`/admin/sync` requires the lead role: sync is an administrative action, and the 403 for junior tokens is itself part of the security demo.

## 15. Observability (`metrics.py`)

**Hand-rolled Prometheus exposition** (~80 lines) instead of `prometheus_client`: full control of the registry lifecycle (resettable in tests), no global-registry conflicts, one less dependency — while staying byte-compatible with the Prometheus text format so the central CERN IT monitoring stack (Prometheus/Grafana) can scrape `/metrics` out of the box.

Exported: `mcp_tool_calls_total{tool}`, `mcp_tool_latency_seconds_sum/_count{tool}` (Prometheus summary convention), `rbac_denials_total{layer}` (one label per security layer — auth, role validation, document ACL), `sync_runs_total{trigger}`, `sync_last_success_timestamp`, and `indexed_documents` / `indexed_chunks` gauges computed at scrape time. `/metrics` is public: counters carry no document content.

## 16. Evaluation Framework (`eval_suite.py`, `eval/golden_dataset.yaml`)

Eight gated scenarios; the harness exits non-zero on any failure:

1–3. Authorized junior / adversarial junior / authorized lead access (PoC scenarios, retained).
4. Context precision on a topical query.
5. Table parsing integrity (every Markdown table vs its HTML source dimensions).
6. **Golden-set retrieval**: 20 query/expected-document pairs in committed YAML; gate `hit_rate@3 ≥ 0.9`.
7. **Adversarial probes**: role-escalation, embedded-injection and over-privilege queries from YAML, checked against both the agent answer and the raw retrieval; gate: 0 leaked markers.
8. **Faithfulness (LLM-as-judge)**: the configured LLM judges whether every claim in sampled answers is supported by the retrieved context; gate ≥ 0.8. Skipped cleanly on the stub backend (no generative claims to judge).

Leak detection matches **restricted content markers** (register addresses, offsets), not refusal phrasing — wording-independent, so it survives any LLM swap.

**Two-speed CI.** Per-push: ruff → `pytest -m "not semantic"` → eval on tfidf/stub (seconds, no torch) + a Trivy HIGH/CRITICAL filesystem scan. Nightly: full dependency stack, complete test suite including the Chroma/embedding tests, eval on semantic + Gemini (judge included). Semantic tests `importorskip` their stack, so the fast path never needs torch installed.

## 17. Container & Deployment

**Dockerfile:** CPU-only torch from the PyTorch index (≈200 MB vs the multi-GB CUDA default); embedding model baked at build time under the non-root user's `HF_HOME`; only `/app/.chroma` is writable by the runtime user, the rest of `/app` stays root-owned; `HEALTHCHECK` uses stdlib urllib (no curl in slim images); port 7860 per HF convention.

**Space provisioning is code** (`scripts/deploy_hf_space.py`): creates the Docker Space, sets secrets (Confluence credentials, Gemini key, token registry) and non-secret variables (backend selection), uploads exactly the allow-listed files, and writes the Space README with HF front-matter. Re-runnable; the Space is reproducible from scratch.

**Confluence seeding is code** (`scripts/seed_confluence.py`): idempotent upserts (version bump on re-run), ACL labels applied per page, hard parsing cases (nested macros, multi-table pages) and the injection fixture included by design.

The space is seeded as a page tree — three section index pages under the homepage (SOPs, Maintenance & Diagnostics, Machine Protection) — so it reads like a real operations space in the Confluence sidebar. The homepage and section indexes deliberately carry **no ACL labels**: they are pure navigation, and the fail-closed gate excludes them on every sync, which keeps the index free of low-content pages that could dilute top-k retrieval. The homepage documents the label→role convention while being itself un-indexed. Aesthetic choices double as parser fixtures: status lozenges inside table cells must survive sanitization as bracketed text (`[OPERATIONAL]`), `toc` and `children` macros must vanish from the markdown entirely, and a deliberately messy shift-handover page (raw bullet notes, a headerless table with ragged rows) exercises the table normalizer against genuinely unstructured content — the corpus stays believable rather than uniformly polished.

## 18. Production Decision Log

| Decision | Choice | Rejected | Why |
|---|---|---|---|
| Vector store | Chroma embedded | Qdrant/pgvector | No extra service; metadata filters cover ACL pushdown |
| Embeddings | Local MiniLM | Embedding APIs | $0, air-gapped, no quota coupling |
| RAG framework | LlamaIndex | LangChain retrieval | Retrieval-first; custom only where security differentiates |
| Orchestration | LangGraph | Hand-rolled loop | Verifier becomes a visible, inspectable graph node |
| LLM | Gemini tiers + Ollama switch | Single provider | $0 default; one-flag air-gapped mode |
| ACL source (demo) | Confluence labels | Page restrictions | Restrictions unavailable on Cloud Free; identical seam |
| Identity | Bearer→role registry, ContextVar | Client-supplied role | Mirrors OIDC claims; removes PoC weakness |
| Role flags in Chroma | Integers (1) | Booleans | LlamaIndex `MetadataFilter` rejects bool values |
| Metrics | Hand-rolled exposition | prometheus_client | Resettable registry, zero dep, format-compatible |
| DNS-rebinding guard | Disabled | Allow-list hosts | Public reverse-proxied deployment with own auth layer |
| State of record | Confluence | Persistent volume | Disposable container; free tier compatible |
| Sync triggers | startup + in-process daily + GH cron | Webhooks | Ephemeral disk mandates startup sync; cron preferred; no stable webhook receiver needed |
