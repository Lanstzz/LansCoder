from dataclasses import dataclass, field

from lanscoder.app.permission_view import (
    ask_user_choice_for_text,
    ask_user_prompt_text,
)


@dataclass
class _FakeOption:
    id: str
    label: str


@dataclass
class _FakePending:
    kind: str = "ask_user"
    question: str = "Which environment?"
    options: list[_FakeOption] = field(default_factory=list)


def test_ask_user_prompt_text_renders_question_and_options() -> None:
    pending = _FakePending(
        question="Which environment?",
        options=[_FakeOption(id="1", label="dev"), _FakeOption(id="2", label="prod")],
    )

    text = ask_user_prompt_text(pending)

    assert text.splitlines()[0] == "Which environment?"
    assert "[1] dev" in text
    assert "[2] prod" in text


def test_ask_user_prompt_text_without_options_renders_question_only() -> None:
    pending = _FakePending(question="Explain your approach", options=[])

    text = ask_user_prompt_text(pending)

    assert text == "Explain your approach"


def test_ask_user_prompt_text_falls_back_to_id_for_empty_label() -> None:
    pending = _FakePending(options=[_FakeOption(id="7", label="")])

    text = ask_user_prompt_text(pending)

    assert "[1] 7" in text


def test_ask_user_choice_matches_by_index() -> None:
    pending = _FakePending(options=[_FakeOption(id="1", label="dev"), _FakeOption(id="2", label="prod")])

    assert ask_user_choice_for_text("2", pending) == "prod"


def test_ask_user_choice_matches_by_label() -> None:
    pending = _FakePending(options=[_FakeOption(id="1", label="dev"), _FakeOption(id="2", label="prod")])

    assert ask_user_choice_for_text("prod", pending) == "prod"


def test_ask_user_choice_matches_label_with_spaces_and_case() -> None:
    pending = _FakePending(options=[_FakeOption(id="1", label="Allow once"), _FakeOption(id="2", label="deny")])

    assert ask_user_choice_for_text("allow once", pending) == "Allow once"


def test_ask_user_choice_returns_none_for_free_text() -> None:
    pending = _FakePending(options=[_FakeOption(id="1", label="dev"), _FakeOption(id="2", label="prod")])

    assert ask_user_choice_for_text("something else entirely", pending) is None


def test_ask_user_choice_returns_none_when_no_options() -> None:
    pending = _FakePending(options=[])

    assert ask_user_choice_for_text("anything", pending) is None
