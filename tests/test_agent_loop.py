"""Tests for the agentic routing pipeline and the Verifier guardrail."""

import pytest
from src.agent_loop import OperationalAgentSubstrate


@pytest.fixture()
def agent() -> OperationalAgentSubstrate:
    """Provides a fresh agent pipeline."""
    return OperationalAgentSubstrate()


def test_greeting_detection_whole_words(agent: OperationalAgentSubstrate) -> None:
    """Greeting routing must match whole tokens, not substrings."""
    assert agent._is_greeting("Hello")
    assert agent._is_greeting("hi there")
    assert agent._is_greeting("good morning")
    # 'high' contains 'hi' but is a technical query and must trigger retrieval
    assert not agent._is_greeting("BPM hits high")
    assert not agent._is_greeting("What is the pressure threshold for the LHC cryo interlock?")


def test_authorized_query_answered(agent: OperationalAgentSubstrate) -> None:
    """A JUNIOR_OP asking about authorized cryo limits must get a real answer."""
    response = agent.run_turn(
        query="What is the warning pressure threshold for the LHC cryo interlock?",
        username="Operator-Alpha",
    )
    assert "1.2e-5" in response
    assert "Security Exception" not in response


def test_restricted_query_yields_no_data(agent: OperationalAgentSubstrate) -> None:
    """A JUNIOR_OP probing restricted SPS registers must get no register data."""
    response = agent.run_turn(
        query="Provide VME crate base register address for the SPS BA3 BPM.",
        username="Operator-Alpha",
    )
    assert "0xFC000000" not in response


def test_lead_gets_restricted_answer(agent: OperationalAgentSubstrate) -> None:
    """An ATS_CORE_LEAD must receive the restricted register configuration."""
    response = agent.run_turn(
        query="Provide VME crate base register address for the SPS BA3 BPM.",
        username="CERN-AI-Lead",
    )
    assert "0xFC000000" in response


def test_verifier_blocks_injected_leak(agent: OperationalAgentSubstrate) -> None:
    """The Phase 3 guardrail must intercept context contaminated with restricted chunks."""
    response = agent.run_turn(
        query="What is the warning pressure threshold for the LHC cryo interlock?",
        username="Operator-Alpha",
        force_inject_leak=True,
    )
    assert response == "Security Exception: Access Denied to restricted resources."
    assert "0xFC000000" not in response


def test_unregistered_session_rejected(agent: OperationalAgentSubstrate) -> None:
    """Unknown usernames must be rejected before any retrieval happens."""
    response = agent.run_turn(query="anything", username="Mallory")
    assert "Security Exception" in response


def test_sources_footer_lists_unique_pages_with_urls() -> None:
    """The citation footer dedupes by page and only lists chunks that carry a URL."""
    chunks = [
        {"doc_id": "a", "url": "https://x/wiki/spaces/S/pages/1", "text": "t"},
        {"doc_id": "a", "url": "https://x/wiki/spaces/S/pages/1", "text": "t2"},
        {"doc_id": "b", "url": "", "text": "t3"},
    ]
    footer = OperationalAgentSubstrate._build_sources_footer(chunks)
    assert footer == "\n\n**Sources:**\n- [a](https://x/wiki/spaces/S/pages/1)"


def test_sources_footer_empty_without_urls() -> None:
    """The air-gapped local source carries no URLs, so no footer is appended."""
    chunks = [{"doc_id": "a", "url": "", "text": "t"}]
    assert OperationalAgentSubstrate._build_sources_footer(chunks) == ""


def test_context_header_includes_url_when_present() -> None:
    """The URL is woven into the context so any citation is grounded for the judge."""
    chunk = {"doc_id": "a", "space": "S", "url": "https://x/p/1", "similarity_score": 0.5, "text": "body"}
    header = OperationalAgentSubstrate._format_source(chunk)
    assert "URL: https://x/p/1" in header
    chunk_no_url = {"doc_id": "a", "space": "S", "url": "", "similarity_score": 0.5, "text": "body"}
    assert "URL:" not in OperationalAgentSubstrate._format_source(chunk_no_url)
