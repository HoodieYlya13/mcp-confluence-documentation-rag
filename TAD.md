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

## 6. Extension Points

### Connecting to the Real Confluence API

The mock data layer is isolated to `mock_cern_confluence/` and `initialize_substrate()` in `server.py`. To connect to a real Confluence instance:

1. Replace `initialize_substrate()` with a `ConfluenceAPISource` that calls:
   ```
   GET /rest/api/content?expand=body.storage,restrictions&spaceKey=...
   ```
   and maps `restrictions.read.restrictions.group` to `allowed_roles`.

2. Pass the fetched `body.storage.value` (XHTML) directly to `ConfluenceSanitizationEngine.parse_file()` — the parser is already designed to consume raw Confluence storage-format XHTML.

3. **Remove `user_role` from all MCP tool signatures.** In production, derive it server-side from CERN SSO / OIDC claims on the authenticated request. The retrieval and generation RBAC layers require no changes; only the identity source changes.

### Connecting to a Real LLM

`_generate_response()` in `agent_loop.py` is the mock generation layer. Replace it with a call to any LLM API (Anthropic, OpenAI, local Ollama) that receives `system_prompt` (constructed in Phase 2) as the system message and `query` as the user message. Phase 3 (Verifier) runs before the LLM call and does not change.

### Remote MCP Deployment

The server currently uses stdio transport (designed for local subprocess communication). FastMCP supports HTTP/SSE transport for remote deployment:

```python
mcp.run(transport="sse", host="0.0.0.0", port=8000)
```

This is the only change needed to the server for remote deployment. The role derivation from SSO/OIDC (see above) becomes mandatory at this point.
