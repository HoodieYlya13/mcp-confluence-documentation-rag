import pytest
from src.parser import ParsedDocument
from src.retrieval import StructureAwareChunker, role_flag


def make_doc(content: str, doc_id: str = "doc1", roles=None) -> ParsedDocument:
    return ParsedDocument(
        doc_id=doc_id,
        space="ATSOPS",
        allowed_roles=roles or ["JUNIOR_OP", "ATS_CORE_LEAD"],
        last_modified="2026-06-10T12:00:00Z",
        clean_content=content,
        metadata={"title": f"Title of {doc_id}"},
    )


def test_invalid_max_words_rejected():
    with pytest.raises(ValueError):
        StructureAwareChunker(max_words=0)


def test_empty_document_produces_no_chunks():
    chunker = StructureAwareChunker(max_words=50)
    assert chunker.chunk_document(make_doc("")) == []


def test_heading_attached_to_chunk():
    content = "# Cryo Limits\n\nThe warning threshold is 1.2e-5 mbar for Q1."
    chunks = StructureAwareChunker(max_words=50).chunk_document(make_doc(content))

    assert len(chunks) == 1
    assert chunks[0].text.startswith("# Cryo Limits")
    assert "1.2e-5" in chunks[0].text


def test_heading_carried_into_continuation_chunks():
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(5))
    content = f"## Procedures\n\n{paragraphs}"
    chunks = StructureAwareChunker(max_words=60).chunk_document(make_doc(content))

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.text.startswith("## Procedures")


def test_tables_are_never_split():
    rows = "\n".join(f"| sensor_{i} | location_{i} | {i}.0e-5 |" for i in range(40))
    table = f"| ID | Location | Threshold |\n| --- | --- | --- |\n{rows}"
    content = f"# Sensor Map\n\nIntro paragraph.\n\n{table}\n\nTrailing paragraph."
    chunks = StructureAwareChunker(max_words=30).chunk_document(make_doc(content))

    table_chunks = [c for c in chunks if "| sensor_0 |" in c.text]
    assert len(table_chunks) == 1
    assert "| sensor_39 |" in table_chunks[0].text


def test_word_budget_splits_paragraphs():
    content = "\n\n".join("word " * 40 for _ in range(6))
    chunks = StructureAwareChunker(max_words=100).chunk_document(make_doc(content))

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.text.split()) <= 140


def test_chunk_acl_is_copied_not_shared():
    doc = make_doc("# T\n\nSome content here.")
    chunks = StructureAwareChunker(max_words=50).chunk_document(doc)

    chunks[0].allowed_roles.append("MUTATED")
    assert "MUTATED" not in doc.allowed_roles


def test_role_flag_normalization():
    assert role_flag("JUNIOR_OP") == "role_junior_op"
    assert role_flag("ATS-CORE-LEAD") == "role_ats_core_lead"


@pytest.mark.semantic
class TestSemanticVectorIndex:

    @pytest.fixture(scope="class")
    def index(self):
        pytest.importorskip("llama_index.core")
        pytest.importorskip("chromadb")
        from src.retrieval import SemanticVectorIndex
        from src.settings import Settings

        settings = Settings(_env_file=None, chunk_max_words=120)
        semantic_index = SemanticVectorIndex(settings, persistent=False)
        docs = [
            make_doc(
                "# LHC Cryogenic Thresholds\n\nThe vacuum warning threshold for the "
                "Sector 3-4 quadrupole is 1.2e-5 mbar. Interlock trip occurs at 5.0e-5 mbar.",
                doc_id="cryo",
                roles=["JUNIOR_OP", "ATS_CORE_LEAD"],
            ),
            make_doc(
                "# SPS VME Register Map\n\nThe BPM orbit controller base register address "
                "is 0xFC000000 with interrupt level 5 and DMA channel 1.",
                doc_id="sps",
                roles=["ATS_CORE_LEAD"],
            ),
        ]
        semantic_index.add_documents(docs)
        return semantic_index

    def test_relevant_chunk_retrieved(self, index):
        results = index.similarity_search(
            "What is the cryogenic vacuum warning pressure?", top_k=2, user_role="JUNIOR_OP"
        )
        assert results
        assert results[0][0].doc_id == "cryo"
        assert "1.2e-5" in results[0][0].text

    def test_acl_filter_blocks_restricted_chunks(self, index):
        results = index.similarity_search(
            "VME register base address for BPM", top_k=5, user_role="JUNIOR_OP"
        )
        assert all(r[0].doc_id != "sps" for r in results)

    def test_lead_retrieves_restricted_chunks(self, index):
        results = index.similarity_search(
            "VME register base address for BPM", top_k=2, user_role="ATS_CORE_LEAD"
        )
        assert any(r[0].doc_id == "sps" for r in results)
        assert any("0xFC000000" in r[0].text for r in results)

    def test_stale_documents_removed_on_resync(self, index):
        new_docs = [
            make_doc(
                "# Collimator Offsets\n\nJaw offset for TCP.C6L7.B1 is -0.184 mm.",
                doc_id="collimator",
                roles=["ATS_CORE_LEAD"],
            )
        ]
        index.add_documents(new_docs)

        results = index.similarity_search(
            "cryogenic vacuum warning threshold", top_k=5, user_role="ATS_CORE_LEAD"
        )
        assert all(r[0].doc_id == "collimator" for r in results)
