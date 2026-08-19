from lanscoder.context.versions import COMPACTION_STRATEGY_VERSION, SYSTEM_PROMPT_VERSION


def test_context_strategy_versions_are_explicit_strings() -> None:
    versions = [
        SYSTEM_PROMPT_VERSION,
        COMPACTION_STRATEGY_VERSION,
    ]

    assert all(version.startswith("v") for version in versions)
