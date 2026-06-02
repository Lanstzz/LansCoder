from firstcoder.context.content.router import (
    RouteCompactResult,
    RouteCompressor,
    RouteContentType,
    RouteContext,
    RouteCompactRouter,
)
from firstcoder.context.content.search import SearchResultsRouteCompressor
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


def test_search_results_compressor_groups_files_and_records_omissions() -> None:
    content = "\n".join(
        [
            "firstcoder/app.py:1: def old_1(): pass",
            *[f"firstcoder/app.py:{line}: def old_{line}(): pass with repeated context" for line in range(2, 40)],
            "firstcoder/app.py:3: TODO important path",
            "firstcoder/tools.py:10: normal match",
            *[f"firstcoder/tools.py:{line}: normal match with repeated context" for line in range(12, 50)],
            "firstcoder/tools.py:11: ERROR must keep this",
        ]
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(max_matches_per_file=3)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content))

    assert result is not None
    assert result.metadata["content_type"] == "search_results"
    assert result.metadata["compacted_by"] == "l3_search_results"
    assert result.metadata["search_original_matches"] > 70
    assert result.metadata["search_kept_matches"] < result.metadata["search_original_matches"]
    assert "firstcoder/app.py:1:" in result.content
    assert "firstcoder/app.py:3: TODO important path" in result.content
    assert "firstcoder/tools.py:11: ERROR must keep this" in result.content
    assert "omitted" in result.content


def test_search_results_compressor_parses_windows_paths() -> None:
    content = "\n".join(
        rf"C:\repo\firstcoder\app.py:{line}: def run_{line}(): pass with repeated context"
        for line in range(1, 30)
    )
    router = RouteCompactRouter(
        compressors={RouteContentType.SEARCH_RESULTS: SearchResultsRouteCompressor(max_matches_per_file=2)},
        min_original_tokens=1,
    )

    result = router.compact_part(_part(content))

    assert result is not None
    assert result.metadata["search_file_count"] == 1
    assert r"C:\repo\firstcoder\app.py:1:" in result.content
