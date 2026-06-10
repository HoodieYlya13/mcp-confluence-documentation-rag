import logging
import time
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src import metrics
from src.auth import current_role, resolve_role_from_token, role_context
from src.config import KNOWN_ROLES, MOCK_CONFLUENCE_DIR, configure_logging
from src.parser import ParsedDocument
from src.retrieval import build_index
from src.settings import get_settings
from src.sources import ConfluenceAPISource, DocumentSource, LocalFileSource

configure_logging()
logger = logging.getLogger("mcp_server")

mcp = FastMCP(
    "Accelerator Operations Substrate",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

DOCUMENTS: Dict[str, ParsedDocument] = {}
INDEX = build_index(get_settings())


def _build_source() -> DocumentSource:
    settings = get_settings()
    if settings.document_source == "confluence":
        logger.info(
            "Document source: Confluence API.",
            extra={"space_key": settings.confluence_space_key}
        )
        return ConfluenceAPISource(settings)
    logger.info("Document source: local files.", extra={"directory": MOCK_CONFLUENCE_DIR})
    return LocalFileSource(MOCK_CONFLUENCE_DIR)


SOURCE: DocumentSource = _build_source()


def initialize_substrate(trigger: str = "startup") -> None:
    logger.info("Initializing operational database substrate...")

    try:
        parsed_docs: List[ParsedDocument] = SOURCE.fetch_documents()
    except Exception:
        logger.error(
            "Document source unavailable. Serving last-known-good index.",
            exc_info=True,
            extra={"documents_retained": len(DOCUMENTS)}
        )
        return

    if not parsed_docs:
        logger.warning(
            "Document source returned no documents. Serving last-known-good index.",
            extra={"documents_retained": len(DOCUMENTS)}
        )
        return

    DOCUMENTS.clear()
    DOCUMENTS.update({doc.doc_id: doc for doc in parsed_docs})
    INDEX.add_documents(parsed_docs)
    metrics.mark_sync_success(trigger=trigger)
    logger.info(
        "Substrate successfully initialized.",
        extra={"indexed_documents": len(DOCUMENTS), "total_chunks": len(INDEX.chunks)}
    )


initialize_substrate()
metrics.set_gauge_provider("indexed_documents", lambda: float(len(DOCUMENTS)))
metrics.set_gauge_provider("indexed_chunks", lambda: float(len(INDEX.chunks)))


def _authenticated_role() -> str:
    role = current_role()
    if role not in KNOWN_ROLES:
        metrics.inc("rbac_denials_total", {"layer": "role_validation"})
        logger.error(
            "Security Violation: Unknown role resolved for MCP tool call.",
            extra={"user_role": role, "security_violation": True}
        )
        raise ValueError(
            f"Unknown role '{role}'. Expected one of: {sorted(KNOWN_ROLES)}."
        )
    return role


def _instrumented(tool_name: str):
    import functools

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            metrics.inc("mcp_tool_calls_total", {"tool": tool_name})
            started = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                metrics.observe_latency(tool_name, time.perf_counter() - started)

        return wrapper

    return decorator


@mcp.tool()
@_instrumented("list_available_pages")
def list_available_pages() -> List[Dict[str, Any]]:
    """Lists metadata for accessible pages (e.g. SPS beam instrumentation, Linac4 SOPs, LHC cryogenics).

    The caller's role is derived server-side from the authenticated identity
    (bearer token over HTTP, configured identity over stdio) and is never
    supplied by the client.

    Returns:
        A list of dictionaries containing authorized document metadata.
    """
    user_role = _authenticated_role()
    logger.info("MCP Tool Executed: list_available_pages", extra={"user_role": user_role})

    available_pages: List[Dict[str, Any]] = []
    for doc in DOCUMENTS.values():
        if user_role in doc.allowed_roles:
            available_pages.append({
                "doc_id": doc.doc_id,
                "title": doc.metadata.get("title", doc.doc_id),
                "space": doc.space,
                "allowed_roles": doc.allowed_roles,
                "last_modified": doc.last_modified
            })

    return available_pages


@mcp.tool()
@_instrumented("fetch_and_sanitize_page")
def fetch_and_sanitize_page(page_id: str) -> str:
    """Retrieves the sanitized Markdown content of an accelerator operations page, enforcing RBAC.

    Works for pages like lhc_cryo_troubleshooting, linac4_injection_sop, sps_beam_instrumentation.

    Args:
        page_id: Unique identifier of the document (Confluence page id).

    Returns:
        The sanitized markdown content of the page.

    Raises:
        ValueError: If the page does not exist.
        PermissionError: If the authenticated role is not authorized for this page.
    """
    user_role = _authenticated_role()
    logger.info(
        "MCP Tool Executed: fetch_and_sanitize_page",
        extra={"page_id": page_id, "user_role": user_role}
    )

    if page_id not in DOCUMENTS:
        logger.warning(f"Requested page {page_id} not found.")
        raise ValueError(f"Page '{page_id}' does not exist.")

    doc = DOCUMENTS[page_id]

    if user_role not in doc.allowed_roles:
        metrics.inc("rbac_denials_total", {"layer": "document_acl"})
        logger.error(
            "Security Violation: Unauthorized document access attempted.",
            extra={
                "page_id": page_id,
                "user_role": user_role,
                "allowed_roles": doc.allowed_roles,
                "security_violation": True
            }
        )
        raise PermissionError(
            f"Security Exception: Role '{user_role}' is unauthorized to access page '{page_id}'."
        )

    return doc.clean_content


@mcp.tool()
@_instrumented("semantic_search_accelerator")
def semantic_search_accelerator(query: str, top_k: int = 3) -> List[Dict[str, Any]]:
    """Performs an RBAC-secure semantic similarity search over the accelerator operations index.

    Includes documents on LHC cryogenics, SPS beam instrumentation, Linac4, VME crate registers,
    and troubleshooting documentation.

    Args:
        query: The search query.
        top_k: Maximum number of chunks to return (clamped to the range [1, 10]).

    Returns:
        A list of search result chunks with similarity scores, filtered to the
        authenticated session's authorization level.
    """
    user_role = _authenticated_role()
    logger.info(
        "MCP Tool Executed: semantic_search_accelerator",
        extra={"query": query, "user_role": user_role, "top_k": top_k}
    )

    top_k = max(1, min(top_k, 10))
    matches = INDEX.similarity_search(query, top_k=top_k, user_role=user_role)

    serialized_results: List[Dict[str, Any]] = []
    for chunk, score in matches:
        serialized_results.append({
            "doc_id": chunk.doc_id,
            "space": chunk.space,
            "chunk_index": chunk.chunk_index,
            "allowed_roles": chunk.allowed_roles,
            "text": chunk.text,
            "similarity_score": round(score, 4)
        })

    return serialized_results


class BearerTokenAuthMiddleware:

    PUBLIC_PATHS = ("/health", "/metrics")

    def __init__(self, app) -> None:
        self.app = app
        self.logger = logging.getLogger("auth_middleware")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self.PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {key: value for key, value in scope.get("headers", [])}
        auth_header = headers.get(b"authorization", b"").decode()
        token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        role = resolve_role_from_token(token) if token else None

        if role is None:
            metrics.inc("rbac_denials_total", {"layer": "http_auth"})
            self.logger.warning(
                "Rejected unauthenticated HTTP request.",
                extra={"path": scope.get("path", ""), "security_violation": True},
            )
            from starlette.responses import JSONResponse

            response = JSONResponse(
                {"error": "Unauthorized. Supply a valid bearer token."}, status_code=401
            )
            await response(scope, receive, send)
            return

        with role_context(role):
            await self.app(scope, receive, send)


async def _sync_scheduler() -> None:
    import asyncio

    interval_seconds = get_settings().sync_interval_hours * 3600
    logger.info(
        "In-process sync scheduler started.",
        extra={"interval_hours": get_settings().sync_interval_hours},
    )
    while True:
        await asyncio.sleep(interval_seconds)
        logger.info("Scheduled sync triggered.")
        await asyncio.to_thread(initialize_substrate)


def build_http_app():
    import asyncio
    import contextlib

    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()

    async def health(request):
        return JSONResponse({
            "status": "ok",
            "indexed_documents": len(DOCUMENTS),
            "total_chunks": len(INDEX.chunks),
            "retriever_backend": type(INDEX).__name__,
        })

    async def admin_sync(request):
        role = current_role()
        if role != "ATS_CORE_LEAD":
            logger.warning(
                "Rejected admin sync request from non-lead role.",
                extra={"user_role": role, "security_violation": True},
            )
            return JSONResponse(
                {"error": "Sync requires the ATS_CORE_LEAD role."}, status_code=403
            )
        await asyncio.to_thread(initialize_substrate, "admin_endpoint")
        report = getattr(SOURCE, "last_report", None)
        return JSONResponse({
            "status": "synced",
            "indexed_documents": len(DOCUMENTS),
            "total_chunks": len(INDEX.chunks),
            "sync_report": report.__dict__ if report else None,
        })

    async def metrics_endpoint(request):
        from starlette.responses import PlainTextResponse

        return PlainTextResponse(
            metrics.render_prometheus(), media_type="text/plain; version=0.0.4"
        )

    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    app.router.routes.insert(0, Route("/metrics", metrics_endpoint, methods=["GET"]))
    app.router.routes.insert(0, Route("/admin/sync", admin_sync, methods=["POST"]))

    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan_with_scheduler(app_):
        async with original_lifespan(app_):
            scheduler = asyncio.create_task(_sync_scheduler())
            try:
                yield
            finally:
                scheduler.cancel()

    app.router.lifespan_context = lifespan_with_scheduler
    return BearerTokenAuthMiddleware(app)


def main() -> None:
    settings = get_settings()
    if settings.mcp_transport == "streamable-http":
        import uvicorn

        logger.info(
            "Starting MCP server over streamable HTTP.",
            extra={"host": settings.http_host, "port": settings.http_port},
        )
        uvicorn.run(
            build_http_app(),
            host=settings.http_host,
            port=settings.http_port,
            log_config=None,
        )
    else:
        logger.info("Starting MCP server over stdio.")
        mcp.run()


if __name__ == "__main__":
    main()
