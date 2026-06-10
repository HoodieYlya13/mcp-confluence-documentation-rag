import pytest
from src import metrics
from src.auth import role_context
from src.config import SecurityRoles


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics.reset_for_testing()
    yield
    metrics.reset_for_testing()


def test_counter_increment_and_render():
    metrics.inc("mcp_tool_calls_total", {"tool": "semantic_search_accelerator"})
    metrics.inc("mcp_tool_calls_total", {"tool": "semantic_search_accelerator"})
    output = metrics.render_prometheus()

    assert '# TYPE mcp_tool_calls_total counter' in output
    assert 'mcp_tool_calls_total{tool="semantic_search_accelerator"} 2.0' in output


def test_latency_observation():
    metrics.observe_latency("fetch_and_sanitize_page", 0.25)
    metrics.observe_latency("fetch_and_sanitize_page", 0.75)
    output = metrics.render_prometheus()

    assert 'mcp_tool_latency_seconds_sum{tool="fetch_and_sanitize_page"} 1.0' in output
    assert 'mcp_tool_latency_seconds_count{tool="fetch_and_sanitize_page"} 2.0' in output


def test_gauge_provider_rendered():
    metrics.set_gauge_provider("indexed_documents", lambda: 7.0)
    output = metrics.render_prometheus()

    assert "# TYPE indexed_documents gauge" in output
    assert "indexed_documents 7.0" in output


def test_sync_success_marks_timestamp():
    metrics.mark_sync_success(trigger="startup")
    output = metrics.render_prometheus()

    assert 'sync_runs_total{trigger="startup"} 1.0' in output
    assert "sync_last_success_timestamp" in output


def test_metrics_endpoint_public_and_instrumented():
    from src.server import DOCUMENTS, build_http_app, semantic_search_accelerator
    from starlette.testclient import TestClient

    metrics.set_gauge_provider("indexed_documents", lambda: float(len(DOCUMENTS)))
    with role_context(SecurityRoles.JUNIOR_OP):
        semantic_search_accelerator(query="cryo vacuum threshold")

    client = TestClient(build_http_app())
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "mcp_tool_calls_total" in response.text
    assert "indexed_documents" in response.text


def test_rbac_denial_counted():
    from src.server import fetch_and_sanitize_page

    with role_context(SecurityRoles.JUNIOR_OP):
        with pytest.raises(PermissionError):
            fetch_and_sanitize_page(page_id="sps_beam_instrumentation")

    output = metrics.render_prometheus()
    assert 'rbac_denials_total{layer="document_acl"} 1.0' in output
