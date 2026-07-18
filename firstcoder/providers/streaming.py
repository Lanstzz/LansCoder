"""Shared helpers for adapting synchronous provider streams to async consumers."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any


def read_field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK object or a test-friendly dictionary."""

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True, slots=True)
class StreamFailure:
    error: BaseException


STREAM_ENDED = object()


def close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # Closing a cancelled stream is best effort; it must not prevent
            # the worker from publishing its terminal sentinel.
            pass


def start_sync_stream_worker(
    stream: Any,
    *,
    thread_name: str,
) -> tuple[queue.Queue[Any], Callable[[], None]]:
    """Read one synchronous iterator in a dedicated thread and expose a queue."""

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
