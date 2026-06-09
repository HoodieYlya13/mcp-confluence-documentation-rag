"""Tests for the ConfluenceSanitizationEngine ETL pipeline."""

import os

import pytest
from bs4 import BeautifulSoup
from src.config import MOCK_CONFLUENCE_DIR
from src.parser import ConfluenceSanitizationEngine, ParsedDocument


@pytest.fixture()
def engine() -> ConfluenceSanitizationEngine:
    """Provides a fresh sanitization engine instance."""
    return ConfluenceSanitizationEngine()


@pytest.fixture()
def cryo_doc(engine: ConfluenceSanitizationEngine) -> ParsedDocument:
    """Parses the LHC cryo troubleshooting mock document."""
    path = os.path.join(MOCK_CONFLUENCE_DIR, "lhc_cryo_troubleshooting.html")
    return engine.parse_file(path)


def test_metadata_extraction(cryo_doc: ParsedDocument) -> None:
    """ACL metadata must be extracted from the leading HTML comment block."""
    assert cryo_doc.doc_id == "lhc_cryo_troubleshooting"
    assert cryo_doc.space == "LHC-CRYO"
    assert cryo_doc.allowed_roles == ["JUNIOR_OP", "ATS_CORE_LEAD"]
    assert cryo_doc.last_modified == "2026-06-08T12:00:00Z"


def test_macros_are_stripped(cryo_doc: ParsedDocument) -> None:
    """No raw Atlassian macro markup may survive sanitization."""
    assert "ac:structured-macro" not in cryo_doc.clean_content
    assert "ac:rich-text-body" not in cryo_doc.clean_content
    assert "ac:parameter" not in cryo_doc.clean_content
    # Macro titles must be preserved as bold markers
    assert "[Operational Notice: LHC Sector 3-4 Cryogenic Interlock]" in cryo_doc.clean_content


def test_headings_preserved_as_atx(cryo_doc: ParsedDocument) -> None:
    """Document typography hierarchy must survive as ATX-style headings."""
    assert "# LHC Cryo-Vacuum Troubleshooting & Interlock Thresholds" in cryo_doc.clean_content
    assert "## 1. Vacuum Sensor Thresholds" in cryo_doc.clean_content


def test_table_converted_to_markdown(cryo_doc: ParsedDocument) -> None:
    """HTML sensor tables must become well-formed Markdown tables."""
    lines = [line.strip() for line in cryo_doc.clean_content.splitlines()]
    table_lines = [line for line in lines if line.startswith("|") and line.endswith("|")]
    # 1 header + 1 divider + 3 data rows
    assert len(table_lines) == 5
    header_cols = table_lines[0].count("|") - 1
    assert header_cols == 5
    assert all(line.count("|") - 1 == header_cols for line in table_lines)


def test_technical_identifiers_not_escaped(cryo_doc: ParsedDocument) -> None:
    """Sensor IDs and scientific notation must survive verbatim (no backslashes)."""
    assert "VGPB_34_Q1" in cryo_doc.clean_content
    assert "1.2e-5" in cryo_doc.clean_content
    assert "\\_" not in cryo_doc.clean_content
    assert "\\|" not in cryo_doc.clean_content


def test_missing_metadata_raises(tmp_path, engine: ConfluenceSanitizationEngine) -> None:
    """Documents without an ACL metadata header must be rejected, not indexed."""
    bad_file = tmp_path / "no_metadata.html"
    bad_file.write_text("<h1>Orphan page with no ACL header</h1>", encoding="utf-8")
    with pytest.raises(ValueError):
        engine.parse_file(str(bad_file))


def test_nested_macros_fully_sanitized(engine: ConfluenceSanitizationEngine) -> None:
    """Macros nested inside other macros' bodies must also be unwrapped."""
    html = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>outer</p>'
        '<ac:structured-macro ac:name="note"><ac:rich-text-body><p>inner</p>'
        "</ac:rich-text-body></ac:structured-macro>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    soup = BeautifulSoup(html, "html.parser")
    engine._sanitize_macros(soup)
    rendered = str(soup)
    assert "ac:structured-macro" not in rendered
    assert "outer" in rendered
    assert "inner" in rendered


def test_all_mock_documents_parse(engine: ConfluenceSanitizationEngine) -> None:
    """Every shipped mock Confluence export must parse without raising."""
    html_files = [f for f in os.listdir(MOCK_CONFLUENCE_DIR) if f.endswith(".html")]
    assert len(html_files) == 3
    for filename in html_files:
        doc = engine.parse_file(os.path.join(MOCK_CONFLUENCE_DIR, filename))
        assert doc.clean_content
        assert doc.allowed_roles
