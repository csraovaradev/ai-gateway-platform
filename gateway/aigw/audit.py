"""Structured audit log: one JSON line per request. Records metadata only, never prompt content."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_logger = logging.getLogger("aigw.audit")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def record(**fields: Any) -> dict[str, Any]:
    event = {"ts": datetime.now(UTC).isoformat(), "event": "chat_completion", **fields}
    _logger.info(json.dumps(event, separators=(",", ":")))
    return event
