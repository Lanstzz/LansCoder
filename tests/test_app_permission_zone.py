from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lanscoder.app.tui import LansCoderApp, LansCoderTuiConfig


class _PendingRunner:
    def __init__(self, pending):
        self.last_pending_input = pending
        self._resumed = []

    async def aresume_with_user_input(self, request_id, answer):
        self._resumed.append((request_id, answer))
        return SimpleNamespace(text="done", model="m", provider="p")

    async def achat(self, **kwargs):
        return SimpleNamespace(text="x", model="m", provider="p")


def _pending(kind, *, options=(), payload=None):
    return SimpleNamespace(
        id="req-1",
        kind=kind,
        question="允许执行吗？",
        options=[SimpleNamespace(id=oid, label=olabel) for oid, olabel in options],
        payload=payload or {},
    )


def _make_app(pending):
    runner = _PendingRunner(pending)
    app = LansCoderApp(config=LansCoderTuiConfig(), chat_runner=runner, command_handler=Mock())
    return app, runner


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_permission_zone_shows_buttons_for_options():
    pending = _pending("permission_confirmation", options=[("deny", "deny"), ("allow_once", "allow_once")])
    app, _runner = _make_app(pending)
    async with app.run_test() as pilot:
        await pilot.pause()
        zone = _query_zone(app)
        assert zone is not None and not zone.has_class("hidden")
        buttons = _zone_buttons(app)
        assert {b.label for b in buttons} == {"deny", "allow once"}


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_permission_button_click_submits_choice():
    pending = _pending("permission_confirmation", options=[("deny", "deny"), ("allow_once", "allow_once")])
    app, runner = _make_app(pending)
    async with app.run_test() as pilot:
        await pilot.pause()
        buttons = _zone_buttons(app)
        allow = next(b for b in buttons if b.id == "permission-allow_once")
        allow.press()
        await pilot.pause()
        assert runner._resumed == [("req-1", "allow_once")]


def _query_zone(app):
    try:
        return app.query_one("#permission-zone")
    except Exception:
        return None


def _zone_buttons(app):
    try:
        return list(app.query("#permission-zone Button"))
    except Exception:
        return []