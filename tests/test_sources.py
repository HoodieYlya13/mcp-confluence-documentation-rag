from typing import Dict, List

import httpx
import pytest
from src.parser import ConfluenceSanitizationEngine
from src.settings import Settings
from src.sources import ConfluenceAPISource, ConnectorError, LocalFileSource


def make_settings() -> Settings:
    return Settings(
        document_source="confluence",
        confluence_url="https://test.atlassian.net",
        confluence_email="test@example.com",
        confluence_api_token="token",
        confluence_space_key="ATSOPS",
        _env_file=None,
    )


def make_page(page_id: str, title: str, labels: List[str], version: int = 1) -> Dict[str, object]:
    return {
        "id": page_id,
        "title": title,
        "version": {"number": version, "when": "2026-06-10T12:00:00Z"},
        "metadata": {"labels": {"results": [{"name": label} for label in labels]}},
        "body": {"storage": {"value": f"<h1>{title}</h1><p>Operational content for {title}.</p>"}},
    }


def transport_for_pages(pages: List[Dict[str, object]], limit: int = 25) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        start = int(params.get("start", 0))
        window = pages[start : start + limit]
        return httpx.Response(200, json={"results": window})

    return httpx.MockTransport(handler)


def test_pages_with_acl_labels_are_ingested():
    pages = [make_page("100", "Cryo Guide", ["acl-junior-op", "acl-ats-core-lead"])]
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages(pages), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert len(docs) == 1
    assert docs[0].doc_id == "100"
    assert docs[0].allowed_roles == ["ATS_CORE_LEAD", "JUNIOR_OP"]
    assert docs[0].metadata["title"] == "Cryo Guide"
    assert "# Cryo Guide" in docs[0].clean_content


def test_source_url_prefers_webui_link():
    page = make_page("100", "Cryo Guide", ["acl-junior-op"])
    page["_links"] = {"webui": "/spaces/ATSOPS/pages/100/Cryo+Guide"}
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages([page]), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert docs[0].source_url == (
        "https://test.atlassian.net/wiki/spaces/ATSOPS/pages/100/Cryo+Guide"
    )


def test_source_url_falls_back_when_link_absent():
    page = make_page("200", "SOP", ["acl-junior-op"])
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages([page]), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert docs[0].source_url == (
        "https://test.atlassian.net/wiki/spaces/ATSOPS/pages/200"
    )


def test_pages_without_acl_labels_fail_closed():
    pages = [
        make_page("100", "Labeled", ["acl-junior-op"]),
        make_page("200", "Unlabeled", []),
        make_page("300", "Unknown Label", ["random-tag"]),
    ]
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages(pages), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert [d.doc_id for d in docs] == ["100"]
    assert source.last_report.pages_skipped_no_acl == 2
    assert source.last_report.pages_checked == 3


def test_pagination_fetches_all_pages():
    pages = [make_page(str(i), f"Page {i}", ["acl-junior-op"]) for i in range(60)]
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages(pages), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert len(docs) == 60
    assert source.last_report.pages_checked == 60


def test_incremental_sync_skips_unchanged_versions():
    pages = [make_page("100", "Stable Page", ["acl-junior-op"], version=3)]
    source = ConfluenceAPISource(
        make_settings(), transport=transport_for_pages(pages), retry_backoff_seconds=0
    )

    source.fetch_documents()
    assert source.last_report.pages_changed == 1

    docs = source.fetch_documents()
    assert source.last_report.pages_changed == 0
    assert source.last_report.pages_unchanged == 1
    assert len(docs) == 1


def test_incremental_sync_reparses_new_versions():
    state = {"version": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        page = make_page("100", "Evolving Page", ["acl-junior-op"], version=state["version"])
        return httpx.Response(200, json={"results": [page]})

    source = ConfluenceAPISource(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_seconds=0
    )

    source.fetch_documents()
    state["version"] = 2
    source.fetch_documents()

    assert source.last_report.pages_changed == 1
    assert source.last_report.pages_unchanged == 0


def test_server_errors_are_retried_then_succeed():
    attempts = {"count": 0}
    pages = [make_page("100", "Flaky", ["acl-junior-op"])]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"results": pages})

    source = ConfluenceAPISource(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_seconds=0
    )

    docs = source.fetch_documents()

    assert len(docs) == 1
    assert attempts["count"] == 2


def test_persistent_failure_raises_connector_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    source = ConfluenceAPISource(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_seconds=0
    )

    with pytest.raises(ConnectorError):
        source.fetch_documents()


def test_auth_failure_raises_without_retry():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, text="Unauthorized")

    source = ConfluenceAPISource(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_seconds=0
    )

    with pytest.raises(ConnectorError):
        source.fetch_documents()
    assert attempts["count"] == 1


def test_parse_failure_is_counted_not_fatal():
    class ExplodingParser(ConfluenceSanitizationEngine):
        def parse_content(self, raw_content, metadata):
            if metadata["doc_id"] == "100":
                raise ValueError("simulated parser failure")
            return super().parse_content(raw_content, metadata)

    pages = [
        make_page("100", "Broken", ["acl-junior-op"]),
        make_page("200", "Healthy", ["acl-junior-op"]),
    ]
    source = ConfluenceAPISource(
        make_settings(),
        parser=ExplodingParser(),
        transport=transport_for_pages(pages),
        retry_backoff_seconds=0,
    )

    docs = source.fetch_documents()

    assert [d.doc_id for d in docs] == ["200"]
    assert source.last_report.parse_errors == 1


def test_missing_credentials_rejected(monkeypatch):
    for var in ("CONFLUENCE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(document_source="confluence", _env_file=None)
    with pytest.raises(ConnectorError):
        ConfluenceAPISource(settings)


def test_local_source_reads_mock_directory():
    from src.config import MOCK_CONFLUENCE_DIR

    source = LocalFileSource(MOCK_CONFLUENCE_DIR)
    docs = source.fetch_documents()

    assert len(docs) == 3
    assert {d.doc_id for d in docs} == {
        "lhc_cryo_troubleshooting",
        "linac4_injection_sop",
        "sps_beam_instrumentation",
    }


def test_request_includes_space_and_expand_params():
    captured: Dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    source = ConfluenceAPISource(
        make_settings(), transport=httpx.MockTransport(handler), retry_backoff_seconds=0
    )
    source.fetch_documents()

    assert captured["spaceKey"] == "ATSOPS"
    assert "body.storage" in captured["expand"]
    assert "metadata.labels" in captured["expand"]
    assert "version" in captured["expand"]
