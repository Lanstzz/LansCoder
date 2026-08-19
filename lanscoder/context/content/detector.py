"""L1-L2 程序化压缩使用的内容检测器。"""

from __future__ import annotations

from lanscoder.context.models import MessagePart

COMPACTED_STATES = {
    "archived",
    "trimmed",
    "micro_compacted",
    "route_compacted",
    "l2_route_compacted",
    "checkpointed",
    "pinned",
}


def is_already_compacted(part: MessagePart) -> bool:
    return str(part.metadata.get("compaction_state") or "raw") in COMPACTED_STATES
