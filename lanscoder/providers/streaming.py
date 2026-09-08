from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from lanscoder.providers.types import ProviderDiagnostics, TokenUsage, ToolCall
from lanscoder.utils.json_utils import loads_json_object


def read_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True, slots=True)
class StreamFailure:
    error: BaseException


@dataclass(slots=True)
class StreamToolCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments_text: str = ""
    saw_arguments: bool = False


STREAM_ENDED = object()


def token_usage(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None = None,
    *,
    usage_details: Mapping[str, Any] | None = None,
) -> TokenUsage | None:
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    if input_tokens is None and output_tokens is None and total_tokens is None and not usage_details:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        usage_details=dict(usage_details or {}),
    )


def merge_usage(
    left: TokenUsage | None,
    right: TokenUsage | None,
    *,
    right_is_delta: bool = False,
) -> TokenUsage | None:
    if left is None or right is None:
        return right or left

    if right_is_delta:
        input_tokens = _add_usage_value(left.input_tokens, right.input_tokens)
        output_tokens = _add_usage_value(left.output_tokens, right.output_tokens)
        right_total = right.total_tokens
        if right_total is None and (right.input_tokens is not None or right.output_tokens is not None):
            right_total = (right.input_tokens or 0) + (right.output_tokens or 0)
        total_tokens = _add_usage_value(left.total_tokens, right_total)
        usage_details = _merge_usage_details(left.usage_details, right.usage_details, is_delta=True)
    else:
        input_tokens = right.input_tokens if right.input_tokens is not None else left.input_tokens
        output_tokens = right.output_tokens if right.output_tokens is not None else left.output_tokens
        total_tokens = right.total_tokens if right.total_tokens is not None else left.total_tokens
        if right.total_tokens is None and (right.input_tokens is not None or right.output_tokens is not None):
            if input_tokens is not None and output_tokens is not None:
                total_tokens = input_tokens + output_tokens
        usage_details = _merge_usage_details(left.usage_details, right.usage_details, is_delta=False)

    return token_usage(
        input_tokens,
        output_tokens,
        total_tokens,
        usage_details=usage_details,
    )


def extract_usage_details(
    usage: Any,
    *,
    nested_fields: Iterable[str] = (),
    scalar_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """Extract provider detail fields while excluding non-integer metadata."""

    details: dict[str, Any] = {}
    for name in nested_fields:
        value = read_field(usage, name)
        if isinstance(value, Mapping) or value is not None:
            integer_fields = _integer_usage_fields(value)
            if integer_fields:
                details[name] = integer_fields

    for name in scalar_fields:
        value = read_field(usage, name)
        if isinstance(value, int) and not isinstance(value, bool):
            details[name] = value
    return details


def _integer_usage_fields(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) and not hasattr(value, "__dict__"):
        return {}

    result: dict[str, Any] = {}
    source = value if isinstance(value, Mapping) else vars(value)
    for name, item in source.items():
        if not isinstance(name, str):
            continue
        if isinstance(item, int) and not isinstance(item, bool):
            result[name] = item
        elif isinstance(item, Mapping) or hasattr(item, "__dict__"):
            nested = _integer_usage_fields(item)
            if nested:
                result[name] = nested
    return result


def _add_usage_value(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _merge_usage_details(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    is_delta: bool,
) -> dict[str, Any]:
    if is_delta:
        return _merge_detail_mappings(left, right, add_values=True)
    return _merge_detail_mappings(left, right, add_values=False)


def _merge_detail_mappings(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    add_values: bool,
) -> dict[str, Any]:
    result = dict(left)
    for name, right_value in right.items():
        if not _has_usage_value(right_value):
            continue
        left_value = result.get(name)
        if isinstance(left_value, Mapping) and isinstance(right_value, Mapping):
            result[name] = _merge_detail_mappings(left_value, right_value, add_values=add_values)
        elif add_values and _are_usage_numbers(left_value, right_value):
            result[name] = left_value + right_value
        else:
            result[name] = right_value
    return result


def _has_usage_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, Mapping, list, tuple, set)):
        return bool(value)
    return True


def _are_usage_numbers(left: Any, right: Any) -> bool:
    return isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool)


def complete_stream_tool_calls(
    accumulators: Mapping[int, StreamToolCallAccumulator],
    diagnostics: ProviderDiagnostics,
    *,
    require_identity: bool,
) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for index in sorted(accumulators):
        item = accumulators[index]
        missing_identity = require_identity and (not item.id or not item.name)
        if missing_identity or not item.saw_arguments:
            missing = "id、name 或 arguments" if require_identity else "arguments"
            diagnostics.warnings.append(f"streaming tool_call 缺少 {missing}，已丢弃整组不可执行调用：index={index}, id={item.id}, name={item.name}")
            return []
        arguments = loads_json_object(item.arguments_text)
        if not isinstance(arguments, dict):
            diagnostics.warnings.append(f"streaming tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用：index={index}, id={item.id}, name={item.name}")
            return []
        parsed.append(ToolCall(id=item.id, name=item.name, arguments=arguments))
    return parsed


def close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def start_sync_stream_worker(
    stream: Any,
    *,
    thread_name: str,
) -> tuple[queue.Queue[Any], Callable[[], None]]:
    stream_queue: queue.Queue[Any] = queue.Queue()
    stop_event = threading.Event()
    close_lock = threading.Lock()
    stream_closed = False

    def stop() -> None:
        nonlocal stream_closed

        stop_event.set()
        with close_lock:
            if stream_closed:
                return
            close_stream(stream)
            stream_closed = True

    def worker() -> None:
        try:
            for item in stream:
                if stop_event.is_set():
                    break
                stream_queue.put(item)
        except BaseException as exc:
            stream_queue.put(StreamFailure(exc))
        finally:
            stop()
            stream_queue.put(STREAM_ENDED)

    threading.Thread(target=worker, name=thread_name, daemon=True).start()
    return stream_queue, stop
