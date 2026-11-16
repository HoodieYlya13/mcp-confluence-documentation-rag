from typing import List

import pytest
from src import server
from src.config import SecurityRoles
from src.parser import ParsedDocument
from src.sources import ConnectorError
from starlette.testclient import TestClient

BUNDLED_DOC_IDS = {"lhc_cryo_troubleshooting", "linac4_injection_sop", "sps_beam_instrumentation"}


class UnreachableSource:
    def fetch_documents(self) -> List[ParsedDocument]:
        raise ConnectorError("Confluence API rejected request: 404 on /content.")


class EmptySource:
    def fetch_documents(self) -> List[ParsedDocument]:
        return []


@pytest.fixture(autouse=True)
def restore_substrate(monkeypatch):
    original_source = server.SOURCE
    yield
    monkeypatch.setattr(server, "SOURCE", original_source)
    server.initialize_substrate()


def _start_cold_with(source, monkeypatch) -> None:
    monkeypatch.setattr(server, "SOURCE", source)
    server.DOCUMENTS.clear()
    server.STATUS.is_serving_bundled_fallback = False
    server.initialize_substrate()


@pytest.mark.parametrize("failing_source", [UnreachableSource(), EmptySource()])
def test_cold_start_without_source_serves_bundled_fallback(failing_source, monkeypatch) -> None:
    _start_cold_with(failing_source, monkeypatch)

    assert set(server.DOCUMENTS) == BUNDLED_DOC_IDS
    assert server.STATUS.is_serving_bundled_fallback
    assert server.substrate_health_status() == "degraded"


def test_bundled_fallback_still_enforces_acl(monkeypatch) -> None:
    _start_cold_with(UnreachableSource(), monkeypatch)

    matches = server.INDEX.similarity_search(
        "VME crate register configuration", top_k=10, user_role=SecurityRoles.JUNIOR_OP
    )

    assert all(SecurityRoles.JUNIOR_OP in chunk.allowed_roles for chunk, _ in matches)
    assert "sps_beam_instrumentation" not in {chunk.doc_id for chunk, _ in matches}


def test_source_outage_keeps_last_known_good_index(monkeypatch) -> None:
    indexed_before = set(server.DOCUMENTS)
    monkeypatch.setattr(server, "SOURCE", UnreachableSource())

    server.initialize_substrate("scheduler")

    assert set(server.DOCUMENTS) == indexed_before
    assert not server.STATUS.is_serving_bundled_fallback
    assert server.substrate_health_status() == "ok"


def test_recovered_source_replaces_bundled_fallback(monkeypatch) -> None:
    recovered_source = server.SOURCE
    _start_cold_with(UnreachableSource(), monkeypatch)

    monkeypatch.setattr(server, "SOURCE", recovered_source)
    server.initialize_substrate("scheduler")

    assert not server.STATUS.is_serving_bundled_fallback
    assert server.substrate_health_status() == "ok"


def test_health_endpoint_reports_degraded_fallback(monkeypatch) -> None:
    _start_cold_with(UnreachableSource(), monkeypatch)

    body = TestClient(server.build_http_app()).get("/health").json()

    assert body["status"] == "degraded"
    assert body["serving_bundled_fallback"] is True
    assert body["indexed_documents"] == len(BUNDLED_DOC_IDS)
