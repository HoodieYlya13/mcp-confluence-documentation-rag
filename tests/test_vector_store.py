"""Tests for the LocalVectorIndex chunking, vector math, and RBAC filtering."""

from typing import List

import pytest
from src.parser import ParsedDocument
from src.vector_store import LocalVectorIndex


def _make_doc(doc_id: str, text: str, roles: List[str]) -> ParsedDocument:
    """Builds a minimal ParsedDocument for index tests."""
    return ParsedDocument(
        doc_id=doc_id,
        space=f"SPACE-{doc_id.upper()}",
        allowed_roles=roles,
        last_modified="2026-01-01T00:00:00Z",
        clean_content=text,
        metadata={},
    )


@pytest.fixture()
def index() -> LocalVectorIndex:
    """Provides an index populated with one open and one restricted document."""
    idx = LocalVectorIndex(chunk_size=20, chunk_overlap=5)
    idx.add_documents(
        [
            _make_doc(
                "open_doc",
                "The cryogenic vacuum interlock threshold for the quadrupole magnet "
                "is critical during helium bath operation and beam stability checks.",
                ["JUNIOR_OP", "ATS_CORE_LEAD"],
            ),
            _make_doc(
                "restricted_doc",
                "The VME crate register address for the beam position monitor "
                "controller is restricted hardware configuration data.",
                ["ATS_CORE_LEAD"],
            ),
        ]
    )
    return idx


def test_invalid_chunk_params_rejected() -> None:
    """Overlap >= chunk size would loop forever and must be rejected upfront."""
    with pytest.raises(ValueError):
        LocalVectorIndex(chunk_size=10, chunk_overlap=10)
    with pytest.raises(ValueError):
        LocalVectorIndex(chunk_size=0, chunk_overlap=0)
    with pytest.raises(ValueError):
        LocalVectorIndex(chunk_size=10, chunk_overlap=-1)


def test_sliding_window_overlap() -> None:
    """Consecutive chunks must share exactly `chunk_overlap` words."""
    idx = LocalVectorIndex(chunk_size=10, chunk_overlap=3)
    words = [f"word{i}" for i in range(25)]
    doc = _make_doc("chunky", " ".join(words), ["JUNIOR_OP"])
    chunks = idx.chunk_document(doc)
    assert len(chunks) > 1
    first = chunks[0].text.split()
    second = chunks[1].text.split()
    assert first[-3:] == second[:3]
    # Full text must be reconstructable: no words lost between windows
    assert second[0] == words[7]


def test_chunk_acl_lists_are_isolated(index: LocalVectorIndex) -> None:
    """Mutating one chunk's ACL must not affect siblings or the source document."""
    target = index.chunks[0]
    sibling = index.chunks[-1]
    target.allowed_roles.append("TAMPERED_ROLE")
    assert "TAMPERED_ROLE" not in sibling.allowed_roles


def test_search_returns_relevant_chunk(index: LocalVectorIndex) -> None:
    """Cosine similarity must rank the topically matching document first."""
    results = index.similarity_search("cryogenic vacuum interlock", top_k=2, user_role="ATS_CORE_LEAD")
    assert results
    best_chunk, best_score = results[0]
    assert best_chunk.doc_id == "open_doc"
    assert 0.0 < best_score <= 1.0


def test_rbac_filters_restricted_chunks(index: LocalVectorIndex) -> None:
    """A JUNIOR_OP must never receive chunks from the restricted document."""
    results = index.similarity_search("VME register address", top_k=5, user_role="JUNIOR_OP")
    assert all(chunk.doc_id != "restricted_doc" for chunk, _ in results)


def test_lead_can_access_restricted_chunks(index: LocalVectorIndex) -> None:
    """An ATS_CORE_LEAD must be able to retrieve the restricted document."""
    results = index.similarity_search("VME register address", top_k=5, user_role="ATS_CORE_LEAD")
    assert any(chunk.doc_id == "restricted_doc" for chunk, _ in results)


def test_zero_similarity_chunks_excluded(index: LocalVectorIndex) -> None:
    """Chunks sharing no vocabulary with the query must not be returned."""
    results = index.similarity_search("xylophone zeppelin quasar", top_k=5, user_role="ATS_CORE_LEAD")
    assert results == []


def test_empty_index_returns_no_results() -> None:
    """Searching an unpopulated index must return an empty list, not crash."""
    idx = LocalVectorIndex()
    assert idx.similarity_search("anything", top_k=3, user_role="JUNIOR_OP") == []
