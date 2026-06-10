import threading
import time
from collections import defaultdict
from typing import Callable, Dict, List, Tuple

_lock = threading.Lock()
_counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = defaultdict(float)
_gauge_providers: Dict[str, Callable[[], float]] = {}

_HELP_TEXT = {
    "mcp_tool_calls_total": "Total MCP tool invocations by tool name.",
    "mcp_tool_latency_seconds_sum": "Cumulative MCP tool execution time by tool name.",
    "mcp_tool_latency_seconds_count": "Number of latency observations by tool name.",
    "rbac_denials_total": "Requests denied by an RBAC or authentication layer.",
    "sync_runs_total": "Documentation sync executions by trigger.",
    "sync_last_success_timestamp": "Unix timestamp of the last successful sync.",
    "indexed_documents": "Documents currently indexed.",
    "indexed_chunks": "Chunks currently indexed.",
}


def inc(name: str, labels: Dict[str, str] | None = None, value: float = 1.0) -> None:
    key = (name, tuple(sorted((labels or {}).items())))
    with _lock:
        _counters[key] += value


def observe_latency(tool: str, seconds: float) -> None:
    inc("mcp_tool_latency_seconds_sum", {"tool": tool}, seconds)
    inc("mcp_tool_latency_seconds_count", {"tool": tool})


def set_gauge_provider(name: str, provider: Callable[[], float]) -> None:
    with _lock:
        _gauge_providers[name] = provider


def mark_sync_success(trigger: str) -> None:
    inc("sync_runs_total", {"trigger": trigger})
    key = ("sync_last_success_timestamp", ())
    with _lock:
        _counters[key] = time.time()


def render_prometheus() -> str:
    lines: List[str] = []
    with _lock:
        snapshot = dict(_counters)
        providers = dict(_gauge_providers)

    seen_names = set()
    for (name, labels), value in sorted(snapshot.items()):
        if name not in seen_names:
            seen_names.add(name)
            help_text = _HELP_TEXT.get(name, name)
            metric_type = "gauge" if name.endswith("_timestamp") else "counter"
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {metric_type}")
        if labels:
            label_str = ",".join(f'{key}="{val}"' for key, val in labels)
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    for name, provider in sorted(providers.items()):
        lines.append(f"# HELP {name} {_HELP_TEXT.get(name, name)}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {provider()}")

    return "\n".join(lines) + "\n"


def reset_for_testing() -> None:
    with _lock:
        _counters.clear()
        _gauge_providers.clear()
