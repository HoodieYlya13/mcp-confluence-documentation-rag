import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Dict, Iterator, List, Protocol, Tuple

import httpx

from src.parser import ConfluenceSanitizationEngine, ParsedDocument
from src.settings import Settings


class ConnectorError(Exception):
    pass


@dataclass
class SyncReport:
    pages_checked: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    pages_skipped_no_acl: int = 0
    parse_errors: int = 0


class DocumentSource(Protocol):
    def fetch_documents(self) -> List[ParsedDocument]: ...


class LocalFileSource:

    def __init__(self, directory: str, parser: ConfluenceSanitizationEngine | None = None) -> None:
        self.directory = directory
        self.parser = parser or ConfluenceSanitizationEngine()
        self.logger = logging.getLogger(self.__class__.__name__)

    def fetch_documents(self) -> List[ParsedDocument]:
        if not os.path.isdir(self.directory):
            raise ConnectorError(f"Local document directory not found: {self.directory}")

        documents: List[ParsedDocument] = []
        for filename in sorted(os.listdir(self.directory)):
            if not filename.endswith(".html"):
                continue
            file_path = os.path.join(self.directory, filename)
            try:
                documents.append(self.parser.parse_file(file_path))
            except Exception:
                self.logger.error(
                    f"Failed to parse local document: {filename}",
                    exc_info=True,
                    extra={"filename": filename},
                )
        return documents


class ConfluenceAPISource:

    PAGE_LIMIT = 25
    MAX_RETRIES = 3

    def __init__(
        self,
        settings: Settings,
        parser: ConfluenceSanitizationEngine | None = None,
        transport: httpx.BaseTransport | None = None,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        if not settings.confluence_url or not settings.confluence_email or not settings.confluence_api_token:
            raise ConnectorError(
                "Confluence source requires CONFLUENCE_URL, CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN."
            )
        self.settings = settings
        self.parser = parser or ConfluenceSanitizationEngine()
        self.retry_backoff_seconds = retry_backoff_seconds
        self.logger = logging.getLogger(self.__class__.__name__)
        self.last_report: SyncReport | None = None
        self._cache: Dict[str, Tuple[int, ParsedDocument]] = {}
        self._client = httpx.Client(
            base_url=f"{settings.confluence_url.rstrip('/')}/wiki/rest/api",
            auth=(settings.confluence_email, settings.confluence_api_token),
            timeout=15.0,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def fetch_documents(self) -> List[ParsedDocument]:
        report = SyncReport()
        documents: List[ParsedDocument] = []

        for page in self._iter_pages():
            report.pages_checked += 1
            page_id = str(page["id"])
            version = int(page["version"]["number"])

            labels = [
                label["name"]
                for label in page.get("metadata", {}).get("labels", {}).get("results", [])
            ]
            allowed_roles = sorted(
                {
                    self.settings.acl_label_roles[label]
                    for label in labels
                    if label in self.settings.acl_label_roles
                }
            )
            if not allowed_roles:
                report.pages_skipped_no_acl += 1
                self.logger.warning(
                    "Page skipped: no recognized ACL label. Failing closed.",
                    extra={
                        "page_id": page_id,
                        "title": page.get("title", ""),
                        "labels": labels,
                        "security_violation": True,
                    },
                )
                continue

            cached = self._cache.get(page_id)
            if cached is not None and cached[0] == version:
                documents.append(cached[1])
                report.pages_unchanged += 1
                continue

            metadata = {
                "doc_id": page_id,
                "space": self.settings.confluence_space_key,
                "allowed_roles": allowed_roles,
                "last_modified": page["version"].get("when", ""),
                "title": page.get("title", ""),
                "version": version,
                "source_url": self._page_url(page, page_id),
            }
            body_html = page["body"]["storage"]["value"]
            try:
                document = self.parser.parse_content(body_html, metadata)
            except Exception:
                report.parse_errors += 1
                self.logger.error(
                    "Failed to parse Confluence page.",
                    exc_info=True,
                    extra={"page_id": page_id, "title": page.get("title", "")},
                )
                continue

            self._cache[page_id] = (version, document)
            documents.append(document)
            report.pages_changed += 1

        self.last_report = report
        self.logger.info("Confluence sync completed.", extra=asdict(report))
        return documents

    def _page_url(self, page: Dict[str, object], page_id: str) -> str:
        wiki_base = f"{self.settings.confluence_url.rstrip('/')}/wiki"
        links = page.get("_links")
        web_path = links.get("webui", "") if isinstance(links, dict) else ""
        if web_path:
            return f"{wiki_base}{web_path}"
        return f"{wiki_base}/spaces/{self.settings.confluence_space_key}/pages/{page_id}"

    def _iter_pages(self) -> Iterator[Dict[str, object]]:
        start = 0
        while True:
            response = self._get_with_retry(
                "/content",
                params={
                    "spaceKey": self.settings.confluence_space_key,
                    "type": "page",
                    "status": "current",
                    "expand": "body.storage,metadata.labels,version",
                    "limit": self.PAGE_LIMIT,
                    "start": start,
                },
            )
            payload = response.json()
            results = payload.get("results", [])
            yield from results
            if len(results) < self.PAGE_LIMIT:
                return
            start += self.PAGE_LIMIT

    def _get_with_retry(self, path: str, params: Dict[str, object]) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                response = self._client.get(path, params=params)
                if response.status_code >= 500:
                    raise ConnectorError(f"Confluence server error {response.status_code}.")
                response.raise_for_status()
                return response
            except (httpx.TransportError, ConnectorError) as exc:
                last_error = exc
                wait = self.retry_backoff_seconds * (2 ** attempt)
                self.logger.warning(
                    f"Confluence request failed (attempt {attempt + 1}/{self.MAX_RETRIES}). "
                    f"Retrying in {wait:.1f}s.",
                    extra={"path": path, "error": str(exc)},
                )
                time.sleep(wait)
            except httpx.HTTPStatusError as exc:
                raise ConnectorError(
                    f"Confluence API rejected request: {exc.response.status_code} on {path}."
                ) from exc
        raise ConnectorError(f"Confluence unreachable after {self.MAX_RETRIES} attempts.") from last_error

    def close(self) -> None:
        self._client.close()
