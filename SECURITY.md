# Security Model

This system serves operational documentation with mixed sensitivity levels to AI agents. The design assumption is that **every layer will eventually fail**, so no layer trusts any other.

## Identity

Roles are derived **server-side only** — never accepted from client input:

| Transport | Identity source |
|---|---|
| Streamable HTTP (remote) | `Authorization: Bearer <token>` → server-side token→role registry (`AUTH_TOKENS` secret) |
| stdio (local desktop client) | `STDIO_ROLE` from the launching environment (process-level identity) |

The bearer-token registry deliberately mirrors an **OIDC identity-claim flow**: in a CERN deployment, the ASGI middleware consumes HY13 Passkey SSO ("mini-Keycloak") access tokens instead, and the registry lookup becomes a group-claim mapping. Nothing downstream of `auth.current_role()` changes.

A token that maps to an unknown role is treated as invalid (**fail closed**) and logged as a security event.

### OIDC access tokens (`SSO_ISSUER` / `SSO_AUDIENCE`)

Beyond the static `AUTH_TOKENS` registry, the server also accepts real OIDC access tokens. `SSO_ISSUER`, `SSO_AUDIENCE`, and `SSO_JWKS_URL` each take one value or a bracketed list, so several identity providers are trusted at once (the issuer is read from the token, matched against the allowlist, and its signing keys fetched from the matching JWKS endpoint). Verification is RS256 over the issuer's JWKS, the issuer must be in the allowlist, the token must satisfy `exp`/`iat`, and **either an `aud` entry or the `azp`** (authorized party) must be in `SSO_AUDIENCE` — Keycloak puts the client in `azp` while `aud` carries unrelated resource servers. The `roles` claim is then mapped to a clearance (`ADMIN`/`ADMIN_DURNAL`/`ATS_CORE_LEAD` → `ATS_CORE_LEAD`, `JUNIOR_OP` → `JUNIOR_OP`); any failure fails closed.

### Demo-only signature bypass (`SSO_INSECURE_ISSUER`)

The hosted demo runs on a public Hugging Face Space that **cannot reach the corporate Keycloak** (`miam-keycloak-…durnal.groupe.pharmagest.com`) — it sits behind the company VPN/firewall, so the outbound JWKS fetch the signature check needs is unreachable from the demo's network. To let the multi-provider flow be demonstrated end to end, any issuer listed in `SSO_INSECURE_ISSUER` has its **JWKS/signature verification skipped**. Everything else still applies: the issuer must be in the `SSO_ISSUER` allowlist, the `aud`/`azp` must match, and `exp`/`iat` are enforced (expired tokens are still rejected).

This is an explicit, opt-in, demo-only concession and is safe **in this context only** because the demo environment exposes nothing sensitive: it serves only the bundled, fictional mocked CERN content and no real protected files. **`SSO_INSECURE_ISSUER` must be empty in production.** The production issuer (`auth.hy13dev.com`) is publicly reachable and always goes through full RS256 + JWKS verification — it is never bypassed. The bypass is secure-by-default (empty unless explicitly set) and every use logs a `signature_bypassed` security event.

## Document ACL Provenance

Per-page ACLs are currently encoded as Confluence labels (`acl-junior-op`, `acl-ats-core-lead`) mapped to roles through `Settings` — a deliberate workaround for Confluence Cloud **Free**, which does not support page restrictions. On a paid Atlassian plan (Standard and up), the connector swaps the label reader for Confluence's native authorization data: page restrictions read via `expand=restrictions.read.restrictions` and space permissions, with groups synced from the identity provider (Atlassian Guard / SCIM). Roles then follow automatically from group membership administered in Confluence itself — no label convention to maintain, and a page's ACL can never drift from what its authors see in the Confluence UI. The adapter seam is identical either way, and the fail-closed rule is unchanged: a page whose authorization cannot be resolved is never indexed.

## Four Enforcement Layers

| # | Layer | Location | What it stops |
|---|---|---|---|
| 0 | Bearer auth + role validation | ASGI middleware / `_authenticated_role()` | Unauthenticated requests; invented or mis-mapped roles |
| 1 | ACL filter pushdown | Chroma metadata filter inside the vector query | Restricted chunks ever entering a result set |
| 2 | Context Verifier (LangGraph node) | `verify` node before generation | Restricted chunks reaching the LLM via any retrieval bug or injection |
| 3 | Post-generation leak scan | `generate` node after the LLM call | Register addresses in the answer that are absent from the authorized context (leak or hallucination) |

Ingestion adds a fifth, earlier gate: pages without a recognized ACL label are **never indexed** (validated in production — the space homepage and the navigation-only section pages carry no labels and are excluded on every sync).

## Threat Model

| Attacker | Vector | Stopped by |
|---|---|---|
| Anonymous internet client | Direct HTTP calls to `/mcp` | Layer 0 (401) |
| Authenticated junior operator | Direct `fetch_and_sanitize_page` on a restricted page | Document ACL check (PermissionError, audited) |
| Authenticated junior operator | Semantic queries targeting restricted content | Layer 1 (zero restricted chunks returned) |
| Compromised/buggy retriever | Restricted chunk appears in context | Layer 2 (response aborted with refusal) |
| Malicious page author | Prompt injection embedded in Confluence content | Delimited untrusted-data prompt contract + Layer 3; permanent regression fixture in the corpus and eval gate |
| Roleplay / social engineering | "I am the new ATS core lead…" | Identity comes from the token, not the conversation |
| Misbehaving client | `top_k=10000` index dump | Server-side clamp to [1, 10] |
| Anyone | `POST /admin/sync` | Requires the ATS_CORE_LEAD role (403 otherwise) |

## Auditing

Every denial is structured-logged (JSON) with `"security_violation": true` and counted in Prometheus metrics (`rbac_denials_total{layer=…}`), scrapeable at `/metrics` for Grafana alerting.

## Evaluation Gates

CI fails (non-zero exit) if any of these regress: RBAC leakage rate ≠ 0%, adversarial probe leaks ≠ 0, golden-set hit rate < 90%, faithfulness < 80% (when a generative LLM is configured).

## Secrets Handling

All credentials (Confluence API token, Gemini key, bearer-token registry) live in environment variables / HF Space secrets; `.env` is gitignored and `.env.example` documents the shape without values. Tokens used during development should be rotated before any public handover.
