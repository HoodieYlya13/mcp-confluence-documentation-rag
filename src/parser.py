import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import markdownify
from bs4 import BeautifulSoup


@dataclass
class ParsedDocument:
    doc_id: str
    space: str
    allowed_roles: List[str]
    last_modified: str
    clean_content: str
    source_url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class ConfluenceSanitizationEngine:

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def parse_file(self, file_path: str) -> ParsedDocument:
        self.logger.info("Initiating parsing of Confluence file", extra={"file_path": file_path})

        with open(file_path, encoding="utf-8") as f:
            raw_content = f.read()

        metadata = self._extract_metadata(raw_content)
        return self.parse_content(raw_content, metadata)

    def parse_content(self, raw_content: str, metadata: Dict[str, Any]) -> ParsedDocument:
        doc_id = str(metadata.get("doc_id", ""))
        space = metadata.get("space", "")
        allowed_roles = metadata.get("allowed_roles", [])
        last_modified = metadata.get("last_modified", "")

        if not doc_id or not space or not allowed_roles:
            self.logger.error("Missing critical metadata headers", extra={"metadata": metadata})
            raise ValueError(f"Document '{doc_id or 'unknown'}' is missing required metadata fields.")

        soup = BeautifulSoup(raw_content, "html.parser")
        self._sanitize_macros(soup)
        self._sanitize_tables(soup)

        html_str = str(soup)
        clean_markdown = markdownify.markdownify(
            html_str,
            heading_style="ATX",
            bullets="-",
            strip=["script", "style"],
            escape_asterisks=False,
            escape_underscores=False,
            escape_misc=False,
        )

        clean_markdown = self._post_process_markdown(clean_markdown)

        self.logger.info(
            "Document sanitization completed successfully.",
            extra={"doc_id": doc_id, "space": space, "char_count": len(clean_markdown)}
        )

        return ParsedDocument(
            doc_id=doc_id,
            space=space,
            allowed_roles=allowed_roles,
            last_modified=last_modified,
            clean_content=clean_markdown,
            source_url=str(metadata.get("source_url", "")),
            metadata=metadata
        )

    def _extract_metadata(self, raw_content: str) -> Dict[str, Any]:
        comment_pattern = re.compile(r"<!--\s*(\{.*?\})\s*-->", re.DOTALL)
        match = comment_pattern.search(raw_content)
        if not match:
            self.logger.warning("No JSON metadata comment block found at the top of the file.")
            return {}

        try:
            metadata_dict = json.loads(match.group(1))
            return metadata_dict
        except json.JSONDecodeError:
            self.logger.error("Failed to decode JSON metadata comment block", exc_info=True)
            return {}

    def _sanitize_macros(self, soup: BeautifulSoup) -> None:
        macro_name = re.compile(r"^ac:structured-macro$")
        sanitized_count = 0

        while True:
            macro = soup.find(macro_name)
            if macro is None:
                break

            title = None
            param_tag = macro.find(re.compile(r"^ac:parameter$"), attrs={"ac:name": "title"})
            if param_tag:
                title = param_tag.get_text(strip=True)

            body_tag = macro.find(re.compile(r"^ac:rich-text-body$"))
            body_content = ""
            if body_tag:
                body_content = "".join(str(child) for child in body_tag.children)

            replacement_html = ""
            if title:
                replacement_html += f"<p><strong>[{title}]</strong></p>"
            if body_content:
                replacement_html += body_content

            replacement_soup = BeautifulSoup(replacement_html, "html.parser")
            macro.replace_with(replacement_soup)
            sanitized_count += 1

        for residual in soup.find_all(re.compile(r"^(ac|ri):")):
            residual.unwrap()

        self.logger.debug(f"Sanitized {sanitized_count} Confluence macro tags.")

    def _sanitize_tables(self, soup: BeautifulSoup) -> None:
        tables = soup.find_all("table")
        for table in tables:
            md_table = self._convert_table_to_markdown(table)
            table.replace_with(soup.new_string(md_table))

        self.logger.debug(f"Sanitized {len(tables)} table elements.")

    def _convert_table_to_markdown(self, table_soup: BeautifulSoup) -> str:
        rows = table_soup.find_all("tr")
        if not rows:
            return ""

        md_rows: List[str] = []

        header_row = None
        thead = table_soup.find("thead")
        if thead:
            header_row = thead.find("tr")
        if not header_row and rows:
            header_row = rows[0]

        if not header_row:
            return ""

        headers = [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]
        if not headers:
            return ""

        md_rows.append("| " + " | ".join(headers) + " |")
        md_rows.append("| " + " | ".join(["---"] * len(headers)) + " |")

        data_rows = rows
        if not thead and rows:
            data_rows = rows[1:]

        for r in data_rows:
            if thead and r.parent.name == "thead":
                continue

            cells = r.find_all(["td", "th"])
            if not cells:
                continue

            row_vals = [cell.get_text(strip=True).replace("\n", " ") for cell in cells]

            if len(row_vals) < len(headers):
                row_vals += [""] * (len(headers) - len(row_vals))
            else:
                row_vals = row_vals[:len(headers)]

            md_rows.append("| " + " | ".join(row_vals) + " |")

        return "\n\n" + "\n".join(md_rows) + "\n\n"

    def _post_process_markdown(self, markdown_text: str) -> str:
        cleaned = re.sub(r"\n{3,}", "\n\n", markdown_text)
        lines = [line.rstrip() for line in cleaned.splitlines()]
        return "\n".join(lines).strip()
