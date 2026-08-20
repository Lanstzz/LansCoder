from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lanscoder.context.models import SessionView


def _new_id(prefix: str) -> str:

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_session_id() -> str:
    return _new_id("sess")


def new_message_id() -> str:
    return _new_id("msg")


def new_part_id() -> str:
    return _new_id("part")


def new_event_id() -> str:
    return _new_id("evt")


def new_request_id() -> str:
    return _new_id("req")


def new_checkpoint_id() -> str:
    return _new_id("ckpt")


def stable_json_hash(value: Any, *, length: int = 16) -> str:

    encoded = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def content_fingerprint(text: str, *, length: int = 16) -> str:

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def session_view_fingerprint(view: SessionView) -> str:

    return stable_json_hash(
        {
            "session_id": view.session_id,
            "messages": [message.to_dict() for message in view.messages],
        },
        length=24,
    )


def _json_default(value: Any) -> str:

    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
