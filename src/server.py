import logging
import os
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP

from src.config import KNOWN_ROLES, MOCK_CONFLUENCE_DIR, configure_logging
from src.parser import ConfluenceSanitizationEngine, ParsedDocument
from src.vector_store import LocalVectorIndex

configure_logging()
logger = logging.getLogger("mcp_server")

mcp = FastMCP("Accelerator Operations Substrate")

DOCUMENTS: Dict[str, ParsedDocument] = {}
INDEX = LocalVectorIndex(chunk_size=100, chunk_overlap=25)


def initialize_substrate() -> None:
    global DOCUMENTS, INDEX
    logger.info("Initializing operational database substrate...")

    if not os.path.exists(MOCK_CONFLUENCE_DIR):
        logger.error(f"Confluence directory not found: {MOCK_CONFLUENCE_DIR}")
        return

    parser = ConfluenceSanitizationEngine()
    parsed_docs: List[ParsedDocument] = []

    for filename in os.listdir(MOCK_CONFLUENCE_DIR):
        if filename.endswith(".html"):
            file_path = os.path.join(MOCK_CONFLUENCE_DIR, filename)
            try:
                doc = parser.parse_file(file_path)
                DOCUMENTS[doc.doc_id] = doc
                parsed_docs.append(doc)
            except Exception:
                logger.error(
                    f"Failed to parse document: {filename}",
                    exc_info=True,
                    extra={"filename": filename}
                )

    INDEX.add_documents(parsed_docs)
    logger.info(
        "Substrate successfully initialized.",
        extra={"indexed_documents": len(DOCUMENTS), "total_chunks": len(INDEX.chunks)}
    )


initialize_substrate()


def _validate_role(user_role: str) -> None:
    if user_role not in KNOWN_ROLES:
        logger.error(
            "Security Violation: Unknown role string supplied to MCP tool.",
            extra={"user_role": user_role, "security_violation": True}
        )
        raise ValueError(
            f"Unknown role '{user_role}'. Expected one of: {sorted(KNOWN_ROLES)}."
        )


@mcp.tool()
def list_available_pages(user_role: str) -> List[Dict[str, Any]]:
    """Lists descriptions and metadata for pages accessible to the provided user role.

    Args:
        user_role: The role string of the current session (e.g. JUNIOR_OP, ATS_CORE_LEAD).

    Returns:
        A list of dictionaries containing authorized document metadata.
    """
    logger.info("MCP Tool Executed: list_available_pages", extra={"user_role": user_role})
    _validate_role(user_role)

    available_pages: List[Dict[str, Any]] = []
    for doc in DOCUMENTS.values():
        if user_role in doc.allowed_roles:
            available_pages.append({
                "doc_id": doc.doc_id,
                "space": doc.space,
                "allowed_roles": doc.allowed_roles,
                "last_modified": doc.last_modified
            })

    return available_pages


@mcp.tool()
def fetch_and_sanitize_page(page_id: str, user_role: str) -> str:
    """Retrieves and sanitizes a Confluence page's content, validating RBAC.

    Args:
        page_id: Unique identifier for the Confluence document (e.g. lhc_cryo_troubleshooting).
        user_role: The role string of the current session (e.g. JUNIOR_OP, ATS_CORE_LEAD).

    Returns:
        The sanitized markdown content of the page.

    Raises:
        ValueError: If the page does not exist.
        PermissionError: If the user role does not have authorization to view this page.
    """
    logger.info(
        "MCP Tool Executed: fetch_and_sanitize_page",
        extra={"page_id": page_id, "user_role": user_role}
    )
    _validate_role(user_role)

    if page_id not in DOCUMENTS:
        logger.warning(f"Requested page {page_id} not found.")
        raise ValueError(f"Page '{page_id}' does not exist.")

    doc = DOCUMENTS[page_id]

    if user_role not in doc.allowed_roles:
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
def semantic_search_accelerator(
    query: str, user_role: str, top_k: int = 3
) -> List[Dict[str, Any]]:
    """Performs an RBAC-secure vector similarity search over the document repository.

    Args:
        query: The search query.
        user_role: The role string of the current session (e.g. JUNIOR_OP, ATS_CORE_LEAD).
        top_k: Maximum number of chunks to return (clamped to the range [1, 10]).

    Returns:
        A list of search result chunks with similarity scores.
    """
    logger.info(
        "MCP Tool Executed: semantic_search_accelerator",
        extra={"query": query, "user_role": user_role, "top_k": top_k}
    )
    _validate_role(user_role)

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


if __name__ == "__main__":
    mcp.run()
