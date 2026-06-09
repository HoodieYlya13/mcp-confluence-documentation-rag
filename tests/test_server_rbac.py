"""Security boundary tests for the MCP server tools (zero-leakage RBAC)."""

import pytest
from src.config import SecurityRoles
from src.server import (
    DOCUMENTS,
    fetch_and_sanitize_page,
    list_available_pages,
    semantic_search_accelerator,
)

RESTRICTED_DOC = "sps_beam_instrumentation"


def test_substrate_indexed_all_documents() -> None:
    """All three mock documents must be parsed and registered on startup."""
    assert set(DOCUMENTS) == {
        "lhc_cryo_troubleshooting",
        "linac4_injection_sop",
        RESTRICTED_DOC,
    }


def test_list_pages_hides_restricted_from_junior() -> None:
    """Restricted page metadata must be invisible to JUNIOR_OP."""
    pages = list_available_pages(user_role=SecurityRoles.JUNIOR_OP)
    assert {p["doc_id"] for p in pages} == {"lhc_cryo_troubleshooting", "linac4_injection_sop"}


def test_list_pages_shows_all_to_lead() -> None:
    """ATS_CORE_LEAD must see every indexed page."""
    pages = list_available_pages(user_role=SecurityRoles.ATS_CORE_LEAD)
    assert {p["doc_id"] for p in pages} == set(DOCUMENTS)


def test_fetch_restricted_page_denied_for_junior() -> None:
    """Direct fetch of a restricted page must raise PermissionError for JUNIOR_OP."""
    with pytest.raises(PermissionError):
        fetch_and_sanitize_page(page_id=RESTRICTED_DOC, user_role=SecurityRoles.JUNIOR_OP)


def test_fetch_restricted_page_allowed_for_lead() -> None:
    """ATS_CORE_LEAD must receive the restricted markdown payload."""
    content = fetch_and_sanitize_page(page_id=RESTRICTED_DOC, user_role=SecurityRoles.ATS_CORE_LEAD)
    assert "0xFC000000" in content


def test_fetch_unknown_page_raises() -> None:
    """Unknown page IDs must raise a clear ValueError."""
    with pytest.raises(ValueError):
        fetch_and_sanitize_page(page_id="does_not_exist", user_role=SecurityRoles.ATS_CORE_LEAD)


def test_unknown_role_rejected_at_boundary() -> None:
    """Role strings outside the security model must be rejected by every tool."""
    with pytest.raises(ValueError):
        list_available_pages(user_role="SUPER_ADMIN")
    with pytest.raises(ValueError):
        fetch_and_sanitize_page(page_id=RESTRICTED_DOC, user_role="SUPER_ADMIN")
    with pytest.raises(ValueError):
        semantic_search_accelerator(query="anything", user_role="SUPER_ADMIN")


def test_semantic_search_never_leaks_restricted_chunks() -> None:
    """Targeted adversarial queries must yield zero restricted chunks for JUNIOR_OP."""
    adversarial_queries = [
        "SPS VME BA3 BPM register base address",
        "beam loss monitor DMA channel interrupt level",
        "hardware calibration reset command word",
    ]
    for query in adversarial_queries:
        results = semantic_search_accelerator(query=query, user_role=SecurityRoles.JUNIOR_OP)
        assert all(r["doc_id"] != RESTRICTED_DOC for r in results), (
            f"RBAC leakage detected for query: {query!r}"
        )


def test_unauthorized_role_sees_nothing() -> None:
    """The UNAUTHORIZED role must receive no pages and no search results."""
    assert list_available_pages(user_role=SecurityRoles.UNAUTHORIZED) == []
    assert semantic_search_accelerator(
        query="cryo vacuum interlock threshold", user_role=SecurityRoles.UNAUTHORIZED
    ) == []


def test_search_top_k_is_clamped() -> None:
    """Oversized top_k requests must be clamped, never dump the whole index."""
    results = semantic_search_accelerator(
        query="beam", user_role=SecurityRoles.ATS_CORE_LEAD, top_k=10_000
    )
    assert len(results) <= 10
