"""User-facing multimodal input helpers (attachments, clipboard, paste parsing)."""

from firstcoder.input.attachments import (
    PreparedAttachment,
    UserAttachment,
    attach_path,
    prepare_attachments_for_session,
    resolve_paste_attachments,
)
from firstcoder.input.clipboard import read_clipboard_image_bytes

__all__ = [
    "PreparedAttachment",
    "UserAttachment",
    "attach_path",
    "prepare_attachments_for_session",
    "read_clipboard_image_bytes",
    "resolve_paste_attachments",
]
