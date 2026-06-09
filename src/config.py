import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOCK_CONFLUENCE_DIR = os.path.join(BASE_DIR, "mock_cern_confluence")


class SecurityRoles:
    JUNIOR_OP = "JUNIOR_OP"
    ATS_CORE_LEAD = "ATS_CORE_LEAD"
    UNAUTHORIZED = "UNAUTHORIZED"


KNOWN_ROLES: FrozenSet[str] = frozenset(
    {SecurityRoles.JUNIOR_OP, SecurityRoles.ATS_CORE_LEAD, SecurityRoles.UNAUTHORIZED}
)

USER_SESSIONS: Dict[str, Dict[str, Any]] = {
    "Operator-Alpha": {
        "user_id": "op_alpha",
        "username": "Operator-Alpha",
        "role": SecurityRoles.JUNIOR_OP,
        "description": "Junior Operator in the CERN Control Centre (CCC) cryogenic panel."
    },
    "CERN-AI-Lead": {
        "user_id": "ai_lead",
        "username": "CERN-AI-Lead",
        "role": SecurityRoles.ATS_CORE_LEAD,
        "description": "ATS Core Team Lead and Senior Systems Architect."
    },
    "Intruder-Bot": {
        "user_id": "unauth_bot",
        "username": "Intruder-Bot",
        "role": SecurityRoles.UNAUTHORIZED,
        "description": "External process with no verified CERN credentials."
    }
}


class StructuredJsonFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        created_dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        timestamp = created_dt.isoformat()

        log_payload: Dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "file": record.filename,
            "line": record.lineno,
        }

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        standard_fields = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName"
        }
        for key, value in record.__dict__.items():
            if key not in standard_fields:
                log_payload[key] = value

        return json.dumps(log_payload)


def configure_logging(level: int = logging.INFO) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if root_logger.handlers:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    formatter = StructuredJsonFormatter()
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    logging.info("JSON logging engine successfully initialized for ATS Ops Substrate.")
