from firstcoder.context.content.router import (
    RouteCompactResult,
    RouteCompressor,
    RouteContentType,
    RouteContext,
    RouteCompactRouter,
)
from firstcoder.context.models import MessagePart


class StaticCompressor:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[MessagePart, RouteContext]] = []

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult:
        self.calls.append((part, context))
        return RouteCompactResult(
            content=self.content,
            content_type=RouteContentType.SEARCH_RESULTS,
            compacted_by="test_static",
            metadata={"example": "yes"},
        )


def _part(content: str, *, tool_name: str = "grep") -> MessagePart:
    return MessagePart(
        id="part_1",
        message_id="msg_1",
        kind="text",
        content=content,
        metadata={"tool_name": tool_name},
    )


def test_route_compact_dispatches_by_content_type() -> None:
    compressor = StaticCompressor("[search results compacted]\nmatch_count=2")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )
    part = _part("firstcoder/app.py:10:def run():\nfirstcoder/app.py:20:def stop():")

    result = router.compact_part(part)

    assert result is not None
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["compacted_by"] == "test_static"
    assert compressor.calls[0][0] is part


def test_route_compact_rejects_output_that_is_not_smaller() -> None:
    original = "firstcoder/app.py:10:def run():"
    compressor = StaticCompressor(original + "\nextra metadata that makes output larger")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(original))

    assert result is None


def test_route_compact_adds_metadata() -> None:
    compressor = StaticCompressor("[search results compacted]")
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: compressor},
        min_original_tokens=1,
    )

    result = router.compact_part(_part("firstcoder/app.py:10:def run():\n" * 20))

    assert result is not None
    assert result.id == "part_1"
    assert result.message_id == "msg_1"
    assert result.kind == "text"
    assert result.metadata["compaction_state"] == "route_compacted"
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["route_confidence"] > 0
    assert result.metadata["original_tokens"] > result.metadata["replacement_tokens"]
    assert result.metadata["content_fingerprint"]
    assert result.metadata["compaction_strategy_version"] == "v1"


def test_route_compact_skips_when_no_compressor_is_registered() -> None:
    router = RouteCompactRouter(compressors={}, min_original_tokens=1)

    result = router.compact_part(_part("firstcoder/app.py:10:def run():"))

    assert result is None
