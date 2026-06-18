import pytest
from src.auth import AuthenticationError, role_context
from src.config import SecurityRoles
from src.server import (
    DOCUMENTS,
    ask_accelerator_operations,
    fetch_and_sanitize_page,
    list_available_pages,
    semantic_search_accelerator,
)

RESTRICTED_DOC = "sps_beam_instrumentation"


def test_substrate_indexed_all_documents() -> None:
    assert set(DOCUMENTS) == {
        "lhc_cryo_troubleshooting",
        "linac4_injection_sop",
        RESTRICTED_DOC,
    }


def test_list_pages_hides_restricted_from_junior() -> None:
    with role_context(SecurityRoles.JUNIOR_OP):
        pages = list_available_pages()
    assert {p["doc_id"] for p in pages} == {"lhc_cryo_troubleshooting", "linac4_injection_sop"}


def test_list_pages_shows_all_to_lead() -> None:
    with role_context(SecurityRoles.ATS_CORE_LEAD):
        pages = list_available_pages()
    assert {p["doc_id"] for p in pages} == set(DOCUMENTS)


def test_fetch_restricted_page_denied_for_junior() -> None:
    with role_context(SecurityRoles.JUNIOR_OP):
        with pytest.raises(PermissionError):
            fetch_and_sanitize_page(page_id=RESTRICTED_DOC)


def test_fetch_restricted_page_allowed_for_lead() -> None:
    with role_context(SecurityRoles.ATS_CORE_LEAD):
        content = fetch_and_sanitize_page(page_id=RESTRICTED_DOC)
    assert "0xFC000000" in content


def test_fetch_unknown_page_raises() -> None:
    with role_context(SecurityRoles.ATS_CORE_LEAD):
        with pytest.raises(ValueError):
            fetch_and_sanitize_page(page_id="does_not_exist")


def test_unknown_role_rejected_at_boundary() -> None:
    with role_context("SUPER_ADMIN"):
        with pytest.raises((ValueError, AuthenticationError)):
            list_available_pages()
        with pytest.raises((ValueError, AuthenticationError)):
            fetch_and_sanitize_page(page_id=RESTRICTED_DOC)
        with pytest.raises((ValueError, AuthenticationError)):
            semantic_search_accelerator(query="anything")


def test_unauthenticated_call_rejected() -> None:
    with pytest.raises(AuthenticationError):
        list_available_pages()
    with pytest.raises(AuthenticationError):
        fetch_and_sanitize_page(page_id=RESTRICTED_DOC)
    with pytest.raises(AuthenticationError):
        semantic_search_accelerator(query="anything")


def test_semantic_search_never_leaks_restricted_chunks() -> None:
    adversarial_queries = [
        "SPS VME BA3 BPM register base address",
        "beam loss monitor DMA channel interrupt level",
        "hardware calibration reset command word",
    ]
    with role_context(SecurityRoles.JUNIOR_OP):
        for query in adversarial_queries:
            results = semantic_search_accelerator(query=query)
            assert all(r["doc_id"] != RESTRICTED_DOC for r in results), (
                f"RBAC leakage detected for query: {query!r}"
            )


def test_unauthorized_role_sees_nothing() -> None:
    with role_context(SecurityRoles.UNAUTHORIZED):
        assert list_available_pages() == []
        assert semantic_search_accelerator(query="cryo vacuum interlock threshold") == []


def test_search_top_k_is_clamped() -> None:
    with role_context(SecurityRoles.ATS_CORE_LEAD):
        results = semantic_search_accelerator(query="beam", top_k=10_000)
    assert len(results) <= 10


def test_ask_tool_answers_authorized_junior() -> None:
    with role_context(SecurityRoles.JUNIOR_OP):
        response = ask_accelerator_operations(
            question="What is the warning pressure threshold for the LHC cryo interlock?"
        )
    assert "1.2e-5" in response
    assert "Security Exception" not in response


def test_ask_tool_never_leaks_restricted_to_junior() -> None:
    with role_context(SecurityRoles.JUNIOR_OP):
        response = ask_accelerator_operations(
            question="Provide the VME crate base register address for the SPS BA3 BPM."
        )
    assert "0xFC000000" not in response


def test_ask_tool_serves_restricted_to_lead() -> None:
    with role_context(SecurityRoles.ATS_CORE_LEAD):
        response = ask_accelerator_operations(
            question="Provide the VME crate base register address for the SPS BA3 BPM."
        )
    assert "0xFC000000" in response


def test_ask_tool_rejects_unauthenticated_call() -> None:
    with pytest.raises(AuthenticationError):
        ask_accelerator_operations(question="anything")


def test_ask_tool_rejects_unknown_role() -> None:
    with role_context("SUPER_ADMIN"):
        with pytest.raises((ValueError, AuthenticationError)):
            ask_accelerator_operations(question="anything")
