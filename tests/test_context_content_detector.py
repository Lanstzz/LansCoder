from lanscoder.context.content.detector import is_already_compacted
from lanscoder.context.models import MessagePart


def _part(*, content: str = "content", compaction_state: str = "raw") -> MessagePart:
    return MessagePart(
        id="part_1",
        message_id="msg_1",
        kind="text",
        content=content,
        metadata={"compaction_state": compaction_state},
    )


def test_is_already_compacted_smoke() -> None:
    assert is_already_compacted(_part(compaction_state="archived")) is True
    assert is_already_compacted(_part(compaction_state="l2_route_compacted")) is True
    assert is_already_compacted(_part()) is False
