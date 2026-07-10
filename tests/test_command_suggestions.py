from firstcoder.app.command_suggestions import (
    CommandSuggestionItem,
    CommandSuggestionState,
    builtin_command_suggestion_items,
    query_command_suggestions,
    render_command_suggestions,
    skill_suggestion_items,
)
from firstcoder.skills.models import SkillCatalog, SkillDefinition, SkillSource


def test_query_command_suggestions_matches_skill_description() -> None:
    items = [
        CommandSuggestionItem(
            replacement="/family-office-research",
            label="family-office-research",
            detail="Generate family office research reports 家办研究",
            kind="skill",
        ),
        CommandSuggestionItem(
            replacement="/model",
            label="/model",
            detail="Pick a model",
            kind="command",
        ),
    ]

    state = query_command_suggestions("/家办", items)

    assert state is not None
    assert [item.replacement for item in state.items] == ["/family-office-research"]


def test_render_command_suggestions_marks_selected_item() -> None:
    state = CommandSuggestionState(
        query="/家办",
        items=[
            CommandSuggestionItem(
                replacement="/family-office-research",
                label="family-office-research",
                detail="家办研究",
                kind="skill",
            )
        ],
    )

    rendered = render_command_suggestions(state)

    assert rendered == "Suggestions:\n> family-office-research  skill  家办研究"


def test_accept_command_suggestion_preserves_instruction_text() -> None:
    state = query_command_suggestions(
        "/家办 研究 Walton",
        [
            CommandSuggestionItem(
                replacement="/family-office-research",
                label="family-office-research",
                detail="家办研究",
                kind="skill",
            )
        ],
    )

    assert state is not None
    assert state.accept_selected() == "/family-office-research 研究 Walton"


def test_query_command_suggestions_skips_exact_command_match() -> None:
    state = query_command_suggestions(
        "/skills",
        [
            CommandSuggestionItem(
                replacement="/skills",
                label="/skills",
                detail="Pick a skill",
                kind="command",
            )
        ],
    )

    assert state is None


def test_query_command_suggestions_skips_exact_match_even_with_other_matches() -> None:
    state = query_command_suggestions(
        "/skills",
        [
            CommandSuggestionItem(
                replacement="/skills",
                label="/skills",
                detail="Pick a skill",
                kind="command",
            ),
            CommandSuggestionItem(
                replacement="/skill",
                label="/skill",
                detail="Show skill details",
                kind="command",
            ),
        ],
    )

    assert state is None


def test_builtin_command_suggestion_items_include_help_commands() -> None:
    replacements = [item.replacement for item in builtin_command_suggestion_items()]

    assert "/skills" in replacements
    assert "/model" in replacements


def test_skill_suggestion_items_include_skill_description_and_triggers() -> None:
    catalog = SkillCatalog(
        skills=[
            SkillDefinition(
                name="family-office-research",
                path="family-office-research/SKILL.md",
                source=SkillSource.GLOBAL_AGENT_SKILL,
                root="/skills",
                description="家办研究",
                triggers=("家族办公室",),
            )
        ]
    )

    state = query_command_suggestions("/家族办公室", skill_suggestion_items(catalog))

    assert state is not None
    assert state.items[0].replacement == "/family-office-research"
