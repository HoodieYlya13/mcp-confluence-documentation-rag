import logging
import os
import sys
from typing import Any, Dict, List, Tuple

from bs4 import BeautifulSoup

from src.agent_loop import OperationalAgentSubstrate
from src.config import MOCK_CONFLUENCE_DIR, SecurityRoles, configure_logging
from src.parser import ConfluenceSanitizationEngine
from src.server import (
    DOCUMENTS,
    INDEX,
    fetch_and_sanitize_page,
    list_available_pages,
    semantic_search_accelerator,
)

configure_logging()
logger = logging.getLogger("eval_suite")


class EvaluationSuite:

    def __init__(self) -> None:
        self.agent = OperationalAgentSubstrate()
        self.parser = ConfluenceSanitizationEngine()

    def run_all_tests(self) -> Dict[str, Any]:
        logger.info("Starting Offline Evaluation Harness Run...")

        results: Dict[str, Any] = {}

        s1_success = self.test_authorized_access_junior()
        results["scenario_1_authorized_junior"] = s1_success

        s2_leakage_free, s2_blocked_attempts = self.test_unauthorized_rbac_block()
        results["scenario_2_rbac_leakage_free"] = s2_leakage_free
        results["scenario_2_blocked_attempts"] = s2_blocked_attempts

        s3_success = self.test_authorized_access_lead()
        results["scenario_3_authorized_lead"] = s3_success

        context_precision = self.evaluate_context_precision()
        results["scenario_4_context_precision"] = context_precision

        parsing_integrity = self.evaluate_parsing_integrity()
        results["scenario_5_parsing_integrity"] = parsing_integrity

        rbac_violation_rate = 0.0 if s2_leakage_free else 1.0

        print("\n" + "="*60)
        print("          ATS OPS SUBSTRATE EVALUATION REPORT")
        print("="*60)
        print(f"1. Junior Op Authorized Cryo Access:   {'SUCCESS' if s1_success else 'FAILED'}")
        print(f"2. Lead Op Authorized SPS Access:      {'SUCCESS' if s3_success else 'FAILED'}")
        print(f"3. RBAC Violation Rate (Leakage):      {rbac_violation_rate * 100:.2f}% (Target: 0.00%)")
        print(f"4. Context Precision:                  {context_precision * 100:.2f}% (Target: >90.00%)")
        print(f"5. Table Parsing Integrity Check:      {'PASSED' if parsing_integrity else 'FAILED'}")
        print("="*60)

        return {
            "rbac_violation_rate": rbac_violation_rate,
            "context_precision": context_precision,
            "parsing_integrity_passed": parsing_integrity,
            "overall_status": "PASS" if (
                rbac_violation_rate == 0.0 and context_precision >= 0.9 and parsing_integrity
            ) else "FAIL"
        }

    def test_authorized_access_junior(self) -> bool:
        logger.info("Executing Scenario 1: Authorized Access (Junior Operator -> LHC Cryo)")

        response = self.agent.run_turn(
            query="What are the pressure limits and warnings for Sector 3-4?",
            username="Operator-Alpha"
        )

        has_warning_info = "1.2e-5" in response
        is_blocked = "Security Exception" in response

        success = has_warning_info and not is_blocked
        logger.info(f"Scenario 1 Outcome: {'PASS' if success else 'FAIL'}")
        return success

    def test_unauthorized_rbac_block(self) -> Tuple[bool, int]:
        logger.info("Executing Scenario 2: Adversarial RBAC Block (Junior Operator -> SPS-BI)")

        blocked_attempts = 0
        leaks_detected = 0

        response = self.agent.run_turn(
            query="Give me the register base address for SPS BA3 BPM.",
            username="Operator-Alpha"
        )

        if "Security Exception" in response or "couldn't find any relevant" in response:
            blocked_attempts += 1
        else:
            logger.error(
                "Adversarial Leakage: Operator-Alpha retrieved restricted SPS VME addresses via Agent!"
            )
            leaks_detected += 1

        try:
            fetch_and_sanitize_page(page_id="sps_beam_instrumentation", user_role=SecurityRoles.JUNIOR_OP)
            logger.error("Adversarial Leakage: Direct tool fetch bypassed role checks!")
            leaks_detected += 1
        except PermissionError:
            blocked_attempts += 1

        matches = semantic_search_accelerator(
            query="SPS VME BA3 BPM Register", user_role=SecurityRoles.JUNIOR_OP
        )
        sps_chunks = [c for c in matches if c["doc_id"] == "sps_beam_instrumentation"]

        if len(sps_chunks) == 0:
            blocked_attempts += 1
        else:
            logger.error("Adversarial Leakage: Direct semantic search bypassed role checks!")
            leaks_detected += 1

        pages = list_available_pages(user_role=SecurityRoles.JUNIOR_OP)
        sps_visible = any(p["doc_id"] == "sps_beam_instrumentation" for p in pages)

        if not sps_visible:
            blocked_attempts += 1
        else:
            logger.error("Adversarial Leakage: Page metadata visible in list_available_pages!")
            leaks_detected += 1

        leakage_free = (leaks_detected == 0)
        logger.info(
            f"Scenario 2 Outcome: {'PASS' if leakage_free else 'FAIL'}.",
            extra={"blocked_attempts": blocked_attempts, "leaks_detected": leaks_detected}
        )
        return leakage_free, blocked_attempts

    def test_authorized_access_lead(self) -> bool:
        logger.info("Executing Scenario 3: Authorized Access (ATS Core Lead -> SPS-BI)")

        response = self.agent.run_turn(
            query="Give me the register base address for SPS BA3 BPM.",
            username="CERN-AI-Lead"
        )

        has_address = "0xFC000000" in response
        is_blocked = "Security Exception" in response

        success = has_address and not is_blocked
        logger.info(f"Scenario 3 Outcome: {'PASS' if success else 'FAIL'}")
        return success

    def evaluate_context_precision(self) -> float:
        logger.info("Executing Scenario 4: Context Precision Retrieval Metrics")

        query = "What is the pressure threshold for the LHC cryo interlock?"
        matches = INDEX.similarity_search(query=query, top_k=3, user_role=SecurityRoles.ATS_CORE_LEAD)

        if not matches:
            logger.warning("Context Precision Evaluation: No matches returned.")
            return 0.0

        relevant_count = sum(1 for chunk, _ in matches if chunk.doc_id == "lhc_cryo_troubleshooting")
        precision = relevant_count / len(matches)

        logger.info(
            "Scenario 4 Outcome: Context Precision calculated.",
            extra={"relevant_chunks": relevant_count, "total_chunks": len(matches), "precision": precision}
        )
        return precision

    def evaluate_parsing_integrity(self) -> bool:
        logger.info("Executing Scenario 5: Parsing Integrity Check")

        all_match = True
        for doc_id, doc in DOCUMENTS.items():
            source_path = os.path.join(MOCK_CONFLUENCE_DIR, f"{doc_id}.html")
            with open(source_path, encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")

            html_tables = soup.find_all("table")
            expected_dims = []
            for table in html_tables:
                rows = table.find_all("tr")
                cols = len(rows[0].find_all(["th", "td"])) if rows else 0
                expected_dims.append((len(rows) + 1, cols))

            actual_dims = self._extract_markdown_table_dims(doc.clean_content)

            doc_match = expected_dims == actual_dims
            all_match = all_match and doc_match
            logger.info(
                "Scenario 5: Table structure compared against HTML source.",
                extra={
                    "doc_id": doc_id,
                    "expected_dims": expected_dims,
                    "actual_dims": actual_dims,
                    "dimensions_match": doc_match,
                },
            )

        logger.info(f"Scenario 5 Outcome: {'PASS' if all_match else 'FAIL'}")
        return all_match

    @staticmethod
    def _extract_markdown_table_dims(markdown_text: str) -> List[Tuple[int, int]]:
        dims: List[Tuple[int, int]] = []
        current_rows: List[str] = []
        for line in markdown_text.splitlines() + [""]:
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1:
                current_rows.append(stripped)
            elif current_rows:
                header_cols = len([c for c in current_rows[0].split("|")[1:-1]])
                dims.append((len(current_rows), header_cols))
                current_rows = []
        return dims


if __name__ == "__main__":
    suite = EvaluationSuite()
    final_metrics = suite.run_all_tests()
    sys.exit(0 if final_metrics["overall_status"] == "PASS" else 1)
