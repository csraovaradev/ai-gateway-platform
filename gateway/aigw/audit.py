"""Structured audit log: one JSON line per request. Records metadata only, never prompt content."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from . import metrics, tracing

_logger = logging.getLogger("aigw.audit")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def record(**fields: Any) -> dict[str, Any]:
    event = {"ts": datetime.now(UTC).isoformat(), "event": "chat_completion", **fields}
    if trace_id := tracing.current_trace_id():
        event["trace_id"] = trace_id
    _logger.info(json.dumps(event, separators=(",", ":")))
    metrics.observe(event)  # metrics come from the same event, so they cannot drift from the log
    return event
