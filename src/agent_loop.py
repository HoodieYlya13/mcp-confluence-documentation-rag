import logging
import re
from typing import Any, Dict, List

from src.config import USER_SESSIONS, SecurityRoles, configure_logging
from src.server import semantic_search_accelerator

configure_logging()
logger = logging.getLogger("agent_loop")

GREETING_TOKENS = frozenset({"hello", "hi", "hey", "help", "thanks"})
GREETING_PHRASES = ("good morning", "good afternoon", "good evening")

NO_CONTEXT_RESPONSE = (
    "I'm sorry, I couldn't find any relevant operational documents in the database "
    "to answer your question."
)

RESPONSE_ROUTES: List[Dict[str, Any]] = [
    {
        "keywords": ("cryo", "lhc", "vacuum", "pressure", "limit", "warning"),
        "doc_id": "lhc_cryo_troubleshooting",
        "detail_triggers": ("VGPB_34",),
        "detailed_response": (
            "Based on the LHC Cryogenic Troubleshooting documentation (Space: LHC-CRYO):\n\n"
            "- **Warning Threshold**: 1.2e-5 mbar for Sector 3-4 Q1/Q2, and 1.5e-5 mbar for D1.\n"
            "- **Interlock Trip**: 5.0e-5 mbar for Q1/Q2, and 8.0e-5 mbar for D1.\n\n"
            "On Warning: Initiate pumping group override and monitor PT-100 temperatures. "
            "If the trip point is reached, the Beam Interlock System (BIS) will dump the "
            "beam automatically."
        ),
        "summary_response": (
            "Based on LHC Cryo documentation: Sub-2K helium stability is critical. "
            "Vacuum barrier leaks trigger interlocks."
        ),
    },
    {
        "keywords": ("linac", "injection", "foil"),
        "doc_id": "linac4_injection_sop",
        "detail_triggers": ("PRODUCTION_BEAM", "stripping foil"),
        "detailed_response": (
            "Based on the Linac4 H- Ion Beam Injection SOP (Space: LINAC4):\n\n"
            "1. **Operational Phases & Target Intensity**:\n"
            "   - PILOT_BEAM: Intensity of 5.0e11 charges, RF Cavity Phase -45.2 deg.\n"
            "   - PRODUCTION_BEAM: Intensity of 2.5e12 charges, RF Cavity Phase -12.8 deg.\n"
            "2. **Stripping Foil Checklist**:\n"
            "   - Check foil position on BTV monitor.\n"
            "   - Monitor stripping foil temp (must be < 1800K; switch to spare if exceeded).\n"
            "   - Track integrated charge (flag at 1.0e20 protons)."
        ),
        "summary_response": (
            "Based on Linac4 SOP: Injection uses a carbon stripping foil to convert "
            "H- ions to protons (H+)."
        ),
    },
    {
        "keywords": ("sps", "vme", "register", "instrumentation"),
        "doc_id": "sps_beam_instrumentation",
        "detail_triggers": ("VME_SPS", "0xFC000000"),
        "detailed_response": (
            "Based on the restricted SPS Beam Instrumentation Register config (Space: SPS-BI):\n\n"
            "- **BA3 BPM Controller**: Base address `0xFC000000`, Interrupt Level 5, "
            "DMA Channel 1, Max Calib Error 0.02 mm.\n"
            "- **BA4 BLM Controller**: Base address `0xFD100000`, Interrupt Level 4, "
            "DMA Channel 2, Max Calib Error 0.05 Gy/s.\n"
            "- **BA5 BCT Controller**: Base address `0xFE200000`, Interrupt Level 6, "
            "DMA Channel 0, Max Calib Error 0.1%.\n\n"
            "Calibration must only be executed during dedicated machine development (MD) "
            "sessions. Triggering reset (`0x00FF` to base + 0x08) when beam is present "
            "will trigger a false BLM trip and abort the beam."
        ),
        "summary_response": (
            "SPS configuration: Direct hardware calibration can trigger false beam loss "
            "monitor trips."
        ),
    },
]


class VerificationException(Exception):
    pass


class OperationalAgentSubstrate:

    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

    def run_turn(self, query: str, username: str, force_inject_leak: bool = False) -> str:
        session = USER_SESSIONS.get(username)
        if not session:
            self.logger.warning(
                "Unregistered session token. Rejecting request.",
                extra={"username": username, "security_violation": True}
            )
            return "Security Exception: Unregistered session token."

        user_role = session["role"]
        self.logger.info(
            "User session authenticated. Starting agent execution.",
            extra={"username": username, "user_role": user_role, "query": query}
        )

        self.logger.info("Phase 1 [Router]: Evaluating query for document retrieval requirements.")

        retrieval_needed = not self._is_greeting(query)

        retrieved_chunks: List[Dict[str, Any]] = []
        if retrieval_needed:
            self.logger.info("Phase 1 [Router]: Dispatching semantic search query to MCP layer.")
            retrieved_chunks = semantic_search_accelerator(query=query, user_role=user_role)
            self.logger.info(
                "Phase 1 [Router]: Retrieval completed.",
                extra={"chunks_retrieved": len(retrieved_chunks)}
            )
        else:
            self.logger.info("Phase 1 [Router]: Direct greeting or non-technical query. Retrieval skipped.")

        if force_inject_leak:
            self.logger.warning("Adversarial Test: Artificially injecting unauthorized chunk into context.")
            retrieved_chunks.append({
                "doc_id": "sps_beam_instrumentation",
                "space": "SPS-BI",
                "chunk_index": 0,
                "allowed_roles": [SecurityRoles.ATS_CORE_LEAD],
                "text": "RESTRICTED: Base memory address for BPM registers is 0xFC000000. Interrupt level 5.",
                "similarity_score": 0.9999
            })

        self.logger.info("Phase 2 [Integrator]: Weaving retrieved context into system prompt block.")

        context_block = ""
        if retrieved_chunks:
            context_block = "\n".join(
                f"[Source: {chunk['doc_id']} | Space: {chunk['space']} | "
                f"Score: {chunk['similarity_score']}]\n{chunk['text']}\n"
                for chunk in retrieved_chunks
            )

        system_prompt = (
            "You are the CERN BE-CSS Applied AI Assistant. Answer the operator query accurately "
            "using only the provided context. If no context is available, answer from base knowledge.\n"
            f"=== RETRIEVED CONTEXT ===\n{context_block}\n=========================="
        )
        self.logger.debug(f"Generated system prompt: {system_prompt}")

        self.logger.info("Phase 3 [Verifier]: Running security audit on source context chunks.")

        try:
            self._verify_rbac_guardrails(retrieved_chunks, user_role, username)
        except VerificationException:
            self.logger.critical(
                "Phase 3 [Verifier]: RBAC violation detected in context. Aborting response generation!",
                extra={
                    "username": username,
                    "user_role": user_role,
                    "query": query,
                    "security_violation": True
                }
            )
            return "Security Exception: Access Denied to restricted resources."

        self.logger.info("Phase 3 [Verifier]: Context verification successful. Proceeding to generation.")

        response = self._generate_response(query, retrieved_chunks)
        self.logger.info("Agent turn completed successfully.")
        return response

    def _is_greeting(self, query: str) -> bool:
        normalized = query.lower().strip()
        tokens = re.findall(r"[a-z']+", normalized)
        if len(tokens) >= 4:
            return False
        has_greeting_token = any(token in GREETING_TOKENS for token in tokens)
        has_greeting_phrase = any(phrase in normalized for phrase in GREETING_PHRASES)
        return has_greeting_token or has_greeting_phrase

    def _verify_rbac_guardrails(self, chunks: List[Dict[str, Any]], user_role: str, username: str) -> None:
        for index, chunk in enumerate(chunks):
            allowed_roles = chunk.get("allowed_roles", [])
            if user_role not in allowed_roles:
                raise VerificationException(
                    f"Chunk {index} from document '{chunk.get('doc_id')}' requires roles {allowed_roles}, "
                    f"but user '{username}' only has role '{user_role}'."
                )

    def _generate_response(self, query: str, chunks: List[Dict[str, Any]]) -> str:
        if not chunks:
            if self._is_greeting(query):
                return (
                    "Hello! I am the CERN BE-CSS Operations Assistant. "
                    "How can I help you with accelerator operations today?"
                )
            return NO_CONTEXT_RESPONSE

        q_lower = query.lower()

        for route in RESPONSE_ROUTES:
            if not any(keyword in q_lower for keyword in route["keywords"]):
                continue

            has_topic_chunk = any(chunk["doc_id"] == route["doc_id"] for chunk in chunks)
            if not has_topic_chunk:
                return NO_CONTEXT_RESPONSE

            has_detail = any(
                trigger in chunk["text"]
                for chunk in chunks
                for trigger in route["detail_triggers"]
            )
            return route["detailed_response"] if has_detail else route["summary_response"]

        return (
            f"Retrieved Context refers to:\n\n{chunks[0]['text'][:300]}...\n\n"
            "(Context contains matching elements but query details are unspecified)."
        )


def main() -> None:
    print("--- Starting CERN Accelerator Operations Agent Loop Session ---")
    agent = OperationalAgentSubstrate()

    print("\n[Scenario 1] Operator-Alpha (JUNIOR_OP) queries Cryo limits:")
    res1 = agent.run_turn(
        query="What is the warning pressure threshold for the LHC cryo interlock?",
        username="Operator-Alpha"
    )
    print(f"Agent Response:\n{res1}")

    print("\n[Scenario 2] Operator-Alpha (JUNIOR_OP) queries restricted SPS VME registers:")
    res2 = agent.run_turn(
        query="Provide VME crate base register address for the SPS BA3 BPM.",
        username="Operator-Alpha"
    )
    print(f"Agent Response:\n{res2}")

    print("\n[Scenario 3] CERN-AI-Lead (ATS_CORE_LEAD) queries restricted SPS registers:")
    res3 = agent.run_turn(
        query="Provide VME crate base register address for the SPS BA3 BPM.",
        username="CERN-AI-Lead"
    )
    print(f"Agent Response:\n{res3}")

    print("\n[Scenario 4] Operator-Alpha (JUNIOR_OP) queries, with adversarial leak injection:")
    res4 = agent.run_turn(
        query="What is the warning pressure threshold for the LHC cryo interlock?",
        username="Operator-Alpha",
        force_inject_leak=True
    )
    print(f"Agent Response:\n{res4}")


if __name__ == "__main__":
    main()
