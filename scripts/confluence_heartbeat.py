import logging
import sys
from datetime import datetime, timezone

import httpx
from seed_confluence import seeder_from_environment

logger = logging.getLogger("confluence_heartbeat")

HEARTBEAT_TITLE = "Space Heartbeat"


def heartbeat_body(now: datetime) -> str:
    return (
        "<p>Automated keep-alive edit by the nightly workflow. "
        "This page carries no ACL label, so the documentation assistant never indexes it.</p>"
        f"<p>Last heartbeat: <strong>{now.isoformat(timespec='seconds')}</strong></p>"
    )


def main() -> int:
    seeder = seeder_from_environment()
    if seeder is None:
        return 1

    try:
        seeder.ensure_space()
        seeder.upsert_page(
            HEARTBEAT_TITLE,
            heartbeat_body(datetime.now(timezone.utc)),
            labels=[],
            parent_id=seeder.homepage_id(),
        )
    except httpx.HTTPError as exc:
        logger.error(f"Confluence heartbeat failed: {exc}")
        return 1
    finally:
        seeder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
