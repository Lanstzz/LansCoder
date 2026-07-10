"""Slash command and skill suggestion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.widgets import Static

from firstcoder.app.help_commands import HELP_COMMANDS
from firstcoder.skills.models import SkillCatalog


@dataclass(frozen=True, slots=True)
class CommandSuggestionItem:
    replacement: str
    label: str
    detail: str = ""
    kind: str = "command"


@dataclass(slots=True)
class CommandSuggestionState:
    query: str
    items: list[CommandSuggestionItem] = field(default_factory=list)
    selected_index: int = 0

    @property
    def selected_item(self) -> CommandSuggestionItem | None:
        if not self.items:
            return None
        index = max(0, min(self.selected_index, len(self.items) - 1))
        return self.items[index]

    def move(self, delta: int) -> None:
        if not self.items:
            self.selected_index = 0
            return
        self.selected_index = max(0, min(len(self.items) - 1, self.selected_index + delta))

    def accept_selected(self) -> str:
        selected = self.selected_item
        if selected is None:
            return self.query
        prefix, suffix = _split_slash_query(self.query)
        spacing = " " if suffix and not suffix.startswith(" ") else ""
        return f"{selected.replacement}{spacing}{suffix}"


def query_command_suggestions(
    text: str,
    items: list[CommandSuggestionItem],
    *,
    limit: int = 8,
) -> CommandSuggestionState | None:
    prefix, suffix = _split_slash_query(text)
    if not prefix.startswith("/") or prefix == "/":
        return None
    needle = prefix.removeprefix("/").casefold()
    if not needle:
        return None
    matches = [item for item in items if _matches(item, needle)]
    if suffix == "" and any(item.replacement.casefold() == prefix.casefold() for item in matches):
        return None
    if not matches:
        return None
    return CommandSuggestionState(query=text, items=matches[: max(1, limit)])


def builtin_command_suggestion_items() -> list[CommandSuggestionItem]:
    return [
        CommandSuggestionItem(
            replacement=command.split()[0],
            label=command.split()[0],
            detail=description,
            kind="command",
        )
        for command, description in HELP_COMMANDS
    ]


def skill_suggestion_items(catalog: SkillCatalog) -> list[CommandSuggestionItem]:
    return [
        CommandSuggestionItem(
            replacement=f"/{skill.name}",
            label=skill.name,
            detail=" ".join(part for part in [skill.description, " ".join(skill.triggers), skill.path] if part),
            kind="skill",
        )
        for skill in catalog.skills
    ]


def render_command_suggestions(state: CommandSuggestionState) -> str:
    lines = ["Suggestions:"]
    for index, item in enumerate(state.items):
        marker = ">" if index == state.selected_index else " "
        detail = f"  {item.detail}" if item.detail else ""
        lines.append(f"{marker} {item.label}  {item.kind}{detail}")
    return "\n".join(lines)


class CommandSuggestionsView(Static):
    """Reusable view for realtime slash command suggestions."""

    def __init__(
        self,
        content: object = "",
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(content, id=id, classes=classes or "suggestions hidden", markup=False)

    def show_state(self, state: CommandSuggestionState | None) -> None:
        if state is None:
            self.update("")
            self.add_class("hidden")
            return
        self.remove_class("hidden")
        self.update(render_command_suggestions(state))


def _matches(item: CommandSuggestionItem, needle: str) -> bool:
    haystack = " ".join([item.replacement, item.label, item.detail, item.kind]).casefold()
    return needle in haystack


def _split_slash_query(text: str) -> tuple[str, str]:
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return text, ""
    prefix, separator, suffix = stripped.partition(" ")
    return prefix, suffix if separator else ""
