from pathlib import Path

from lanscoder.skills.catalog import (
    SKILL_CATALOG_MAX_CHARS,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_LOAD_INSTRUCTION,
    render_skill_catalog,
)
from lanscoder.skills.discovery import discover_all_skills, discover_project_skills
from lanscoder.skills.models import SkillCatalog, SkillDefinition, SkillSource


def test_discovers_project_skills_and_uses_index_as_context(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".lanscoder" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "INDEX.md").write_text(
        "# Skill Index\n\n| Skill | 触发场景 |\n|---|---|\n| `daily-brief` | 今日资讯 |\n",
        encoding="utf-8",
    )
    skill_dir = skills_dir / "daily-brief"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Daily Brief\n\n生成日报。", encoding="utf-8")

    catalog = discover_project_skills(tmp_path)

    assert catalog.index_content.startswith("# Skill Index")
    assert [skill.path for skill in catalog.skills] == [".lanscoder/skills/daily-brief/SKILL.md"]
    skill = catalog.skills[0]
    assert skill.name == "daily-brief"
    assert skill.description == "Daily Brief"
    assert skill.source == SkillSource.PROJECT
    assert skill.root == str(tmp_path)


def test_discovers_project_skill_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".lanscoder" / "skills" / "fetch-tweet"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fetch-tweet\ndescription: Fetch X/Twitter posts.\n---\n\n# Fetch Tweet\n",
        encoding="utf-8",
    )

    catalog = discover_project_skills(tmp_path)

    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "fetch-tweet"
    assert skill.description == "Fetch X/Twitter posts."
    assert skill.path == ".lanscoder/skills/fetch-tweet/SKILL.md"
    assert skill.source == SkillSource.PROJECT


def test_discovers_quoted_name_and_folded_yaml_description(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".lanscoder" / "skills" / "family-office"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: "family-office-research"\n'
        "description: >\n"
        "  Generate comprehensive family office research.\n"
        "  Use primary sources and verify claims.\n"
        "triggers:\n"
        "  - family office\n"
        "  - 家族办公室\n"
        "---\n\n"
        "# Family Office Research\n",
        encoding="utf-8",
    )

    skill = discover_project_skills(tmp_path).skills[0]

    assert skill.name == "family-office-research"
    assert skill.description == "Generate comprehensive family office research. Use primary sources and verify claims."
    assert skill.triggers == ("family office", "家族办公室")


def test_discovers_frontmatter_triggers(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".lanscoder" / "skills"
    skills_dir.mkdir(parents=True)
    skill_dir = skills_dir / "daily-brief"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\n" "name: daily-brief\n" "description: Generate daily brief.\n" "triggers: 今日资讯, daily news\n" "---\n\n" "# Daily Brief\n",
        encoding="utf-8",
    )

    catalog = discover_project_skills(tmp_path)

    assert catalog.skills[0].triggers == ("今日资讯", "daily news")


def test_non_string_frontmatter_name_and_description_fall_back_safely(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".lanscoder" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname:\n  - invalid\ndescription:\n  nested: invalid\n---\n\n# Review safely\n",
        encoding="utf-8",
    )

    skill = discover_project_skills(tmp_path).skills[0]

    assert skill.name == "review"
    assert skill.description == "Review safely"


def test_discovers_global_skills_from_default_root(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".lanscoder" / "skills" / "mail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: mail\ndescription: Send and search email.\n---\n\n# Mail\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    catalog = discover_all_skills(tmp_path)

    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "mail"
    assert skill.source == SkillSource.GLOBAL
    assert skill.root == str(home / ".lanscoder" / "skills")
    assert skill.path == "mail/SKILL.md"


def test_disable_global_skills(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".lanscoder" / "skills" / "brief"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Brief Writer\n\n写简报。", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    enabled = discover_all_skills(tmp_path)
    assert len(enabled.skills) == 1

    monkeypatch.setenv("LANSCODER_DISABLE_GLOBAL_SKILLS", "1")
    disabled = discover_all_skills(tmp_path)

    assert disabled.skills == []


def test_catalog_fingerprint_changes_when_skill_metadata_changes(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".lanscoder" / "skills"
    skills_dir.mkdir(parents=True)
    skill_dir = skills_dir / "review"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Review\n\n初版。", encoding="utf-8")

    before = discover_project_skills(tmp_path).fingerprint
    skill_file.write_text("# Review Updated\n\n新版。", encoding="utf-8")
    after = discover_project_skills(tmp_path).fingerprint

    assert before != after


def test_resolved_catalog_prefers_project_skill_for_duplicate_name() -> None:
    global_skill = SkillDefinition(
        name="review",
        path="review/SKILL.md",
        source=SkillSource.GLOBAL,
        root="/global",
        description="Global review rules.",
    )
    project_skill = SkillDefinition(
        name="review",
        path=".lanscoder/skills/review/SKILL.md",
        source=SkillSource.PROJECT,
        root="/project",
        description="Project review rules.",
    )

    resolved = SkillCatalog(skills=[global_skill, project_skill]).resolved()

    assert resolved.skills == [project_skill]


def test_render_skill_catalog_hides_filesystem_metadata_and_bounds_whole_lines() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}",
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL,
            root="/Users/example/.lanscoder/skills",
            description=("A long description with\nextra whitespace. " * 20),
        )
        for index in range(100)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))

    assert len(rendered) <= 8_000
    assert "root=" not in rendered
    assert "SKILL.md" not in rendered
    assert "\nextra whitespace" not in rendered
    assert rendered.splitlines()[0].startswith("- skill-000: A long description")
    assert rendered.splitlines()[-1] == SKILL_LOAD_INSTRUCTION
    assert all(line.startswith("- skill-") for line in rendered.splitlines()[:-1])


def test_render_skill_catalog_keeps_every_skill_name_within_budget() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}",
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL,
            root="/global",
            description="A deliberately long description. " * 40,
        )
        for index in range(100)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))

    assert len(rendered) <= SKILL_CATALOG_MAX_CHARS
    assert rendered.splitlines()[-1] == SKILL_LOAD_INSTRUCTION
    for skill in skills:
        assert f"- {skill.name}:" in rendered


def test_render_skill_catalog_keeps_full_description_budget_for_small_catalog() -> None:
    description = "x" * (SKILL_DESCRIPTION_MAX_CHARS + 20)
    skill = SkillDefinition(
        name="review",
        path="review/SKILL.md",
        source=SkillSource.GLOBAL,
        root="/global",
        description=description,
    )

    line = render_skill_catalog(SkillCatalog(skills=[skill])).splitlines()[0]

    assert line == f"- review: {'x' * (SKILL_DESCRIPTION_MAX_CHARS - 3)}..."


def test_render_skill_catalog_extreme_name_overflow_keeps_whole_lines_and_warning() -> None:
    skills = [
        SkillDefinition(
            name=f"skill-{index:03d}-" + "n" * 400,
            path=f"skill-{index:03d}/SKILL.md",
            source=SkillSource.GLOBAL,
            root="/global",
            description="description must not steal name budget",
        )
        for index in range(30)
    ]

    rendered = render_skill_catalog(SkillCatalog(skills=skills))
    lines = rendered.splitlines()

    assert len(rendered) <= SKILL_CATALOG_MAX_CHARS
    assert lines[-1] == SKILL_LOAD_INSTRUCTION
    assert "Skill catalog truncated:" in lines[-2]
    assert all(line.endswith(":") for line in lines[:-2])
    assert all(line in {f"- {skill.name}:" for skill in skills} for line in lines[:-2])


def test_discovers_directory_skill_with_supporting_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".lanscoder" / "skills" / "api-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: api-review\ndescription: Review API changes.\n---\n\n# API Review\n",
        encoding="utf-8",
    )
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "api-spec.md").write_text("# API Spec\n", encoding="utf-8")

    catalog = discover_project_skills(tmp_path)

    assert len(catalog.skills) == 1
    skill = catalog.skills[0]
    assert skill.name == "api-review"
    assert skill.path == ".lanscoder/skills/api-review/SKILL.md"


def test_flat_md_files_in_skills_dir_are_ignored(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".lanscoder" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "brief.md").write_text("# Brief\n\n写简报。", encoding="utf-8")

    catalog = discover_project_skills(tmp_path)

    assert catalog.skills == []
