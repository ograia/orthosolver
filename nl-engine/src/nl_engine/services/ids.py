from datetime import UTC, datetime
from uuid import uuid4


def new_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{uuid4().hex[:8]}"
