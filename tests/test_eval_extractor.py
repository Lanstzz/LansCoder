from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval_harness.cli import main
from eval_harness.replay.extractor import extract_replay_case
from eval_harness.replay.runner import run_offline_case_path
from eval_harness.schema.models import ManifestError, load_case_manifest
from eval_harness.trace.capsule import CapsuleError, read_capsule, write_capsule


def test_extract_session_writes_redacted_manifest_and_encrypted_capsule(tmp_path: Path) -> None:
    source = _write_session(tmp_path / "lanscoder-session.jsonl")
    case_path = tmp_path / "portable" / "case.json"
    capsule_path = tmp_path / "private" / "case.capsule"

    result = extract_replay_case(source, case_path, capsule=capsule_path, passphrase="correct horse", repository_root=tmp_path / "repo")

    portable = case_path.read_text(encoding="utf-8")
    encrypted = capsule_path.read_text(encoding="utf-8")
    assert result.source_kind == "session"
    assert capsule_path.stat().st_mode & 0o777 == 0o600
    assert "TOKEN=history-secret" not in portable
    assert "/private/project" not in portable
    assert "TOKEN=history-secret" not in encrypted
    assert "Implement the change" not in portable
    assert "<CAPSULE:user_input:" in portable
    assert json.loads(portable)["capsule"]["material_sha256"] == result.material_sha256


def test_extract_and_run_cli_uses_passphrase_environment_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_session(tmp_path / "lanscoder-session.jsonl", include_secret=False)
    case_path = tmp_path / "case.json"
    capsule_path = tmp_path / "case.capsule"
    monkeypatch.setenv("LANSCODER_TEST_CAPSULE_PASSWORD", "pw")

    assert main(
        [
            "extract",
            "--source",
            str(source),
            "--output",
            str(case_path),
            "--capsule",
            str(capsule_path),
            "--capsule-passphrase-env",
            "LANSCODER_TEST_CAPSULE_PASSWORD",
        ]
    ) == 0
    assert main(
        [
            "run",
            "--case",
            str(case_path),
            "--capsule",
            str(capsule_path),
            "--capsule-passphrase-env",
            "LANSCODER_TEST_CAPSULE_PASSWORD",
            "--output",
            str(tmp_path / "run"),
        ]
    ) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_extracted_session_case_hydrates_and_replays_current_runtime(tmp_path: Path) -> None:
    source = _write_session(tmp_path / "session.jsonl", include_secret=False)
    case_path = tmp_path / "case.json"
    capsule_path = tmp_path / "case.capsule"
    extract_replay_case(source, case_path, capsule=capsule_path, passphrase="pw")

    hydrated = load_case_manifest(case_path, capsule_path=capsule_path, capsule_passphrase="pw")
    assert hydrated.prompt == "Implement the change."
    assert hydrated.provider_tape[0].tool_calls[0]["arguments"] == {"path": "greeting.txt", "content": "hello\n"}
    result = await run_offline_case_path(case_path, tmp_path / "run", capsule_path=capsule_path, capsule_passphrase="pw")

    assert result.scorecard["passed"] is True
    assert (result.artifacts_path / "greeting.txt").read_text(encoding="utf-8") == "hello\n"


def test_extract_eval_trace_and_harbor_directory(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "sequence": index,
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": event_type,
                    "data": data,
                }
            )
            for index, (event_type, data) in enumerate(
                [
                    ("user_input", {"content": "Fix the bug."}),
                    ("provider_response", {"content": "Fixed.", "tool_calls": [], "finish_reason": "stop"}),
                    ("final_delivery", {"content": "Fixed."}),
                ],
                start=1,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    capsule = tmp_path / "trace.capsule"
    trace_case = extract_replay_case(trace, tmp_path / "trace-case.json", capsule=capsule, passphrase="pw")
    assert trace_case.source_kind == "trace"
    assert load_case_manifest(trace_case.case_path, capsule_path=capsule, capsule_passphrase="pw").prompt == "Fix the bug."

    harbor_dir = tmp_path / "harbor-job"
    harbor_dir.mkdir()
    harbor_trace = harbor_dir / "logs" / "agent" / "lanscoder-session.jsonl"
    harbor_trace.parent.mkdir(parents=True)
    harbor_trace.write_bytes(trace.read_bytes())
    harbor_capsule = tmp_path / "harbor.capsule"
    harbor_case = extract_replay_case(harbor_dir, tmp_path / "harbor-case.json", capsule=harbor_capsule, passphrase="pw")
    assert harbor_case.source_kind == "trace"


def test_capsule_rejects_wrong_passphrase_and_tampering(tmp_path: Path) -> None:
    path = tmp_path / "material.capsule"
    write_capsule(path, {"prompt": "private"}, "pw")

    with pytest.raises(CapsuleError, match="authentication failed"):
        read_capsule(path, "wrong")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["ciphertext"] = envelope["ciphertext"][:-2] + "AA"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(CapsuleError, match="authentication failed"):
        read_capsule(path, "pw")


def test_capsule_case_requires_matching_capsule(tmp_path: Path) -> None:
    source = _write_session(tmp_path / "session.jsonl", include_secret=False)
    case_path = tmp_path / "case.json"
    capsule_path = tmp_path / "case.capsule"
    extract_replay_case(source, case_path, capsule=capsule_path, passphrase="pw")

    portable = load_case_manifest(case_path)
    assert portable.capsule_required is True
    assert portable.prompt.startswith("<CAPSULE:user_input:")
    with pytest.raises(ManifestError, match="must be provided together"):
        load_case_manifest(case_path, capsule_path=capsule_path)
    with pytest.raises(ManifestError, match="authentication failed"):
        load_case_manifest(case_path, capsule_path=capsule_path, capsule_passphrase="wrong")


def _write_session(path: Path, *, include_secret: bool = True) -> Path:
    prompt = "Implement the change."
    if include_secret:
        prompt += " TOKEN=history-secret /private/project"
    events = [
        {
            "id": "evt_1",
            "session_id": "history",
            "type": "session_created",
            "payload": {"title": "history"},
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "evt_2",
            "session_id": "history",
            "type": "user_message",
            "payload": {"message_id": "msg_user", "parts": [{"id": "part_user", "kind": "text", "content": prompt}]},
            "created_at": "2026-01-01T00:00:01Z",
        },
        {
            "id": "evt_3",
            "session_id": "history",
            "type": "assistant_message",
            "payload": {
                "message_id": "msg_assistant_1",
                "parts": [
                    {
                        "id": "part_call",
                        "kind": "tool_call",
                        "content": "",
                        "metadata": {
                            "tool_call_id": "call_1",
                            "tool_name": "write_file",
                            "arguments": {"path": "greeting.txt", "content": "hello\n"},
                        },
                    }
                ],
                "metadata": {"provider": "historical", "model": "historical-model", "finish_reason": "tool_calls"},
            },
            "created_at": "2026-01-01T00:00:02Z",
        },
        {
            "id": "evt_4",
            "session_id": "history",
            "type": "tool_result",
            "payload": {
                "message_id": "msg_tool_1",
                "parts": [
                    {
                        "id": "part_result",
                        "kind": "tool_result",
                        "content": "wrote greeting.txt",
                        "metadata": {"tool_call_id": "call_1", "tool_name": "write_file", "ok": True},
                    }
                ],
            },
            "created_at": "2026-01-01T00:00:03Z",
        },
        {
            "id": "evt_5",
            "session_id": "history",
            "type": "assistant_message",
            "payload": {
                "message_id": "msg_assistant_2",
                "parts": [{"id": "part_final", "kind": "text", "content": "Completed the change."}],
                "metadata": {"provider": "historical", "model": "historical-model", "finish_reason": "stop"},
            },
            "created_at": "2026-01-01T00:00:04Z",
        },
    ]
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")
    return path
