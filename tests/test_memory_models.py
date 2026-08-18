import pytest

from lanscoder.memory.models import (
    MEMORY_TYPES,
    MemoryRecord,
    MemoryScope,
    deserialize,
    file_content,
    valid_name,
    validate_record,
)


def test_memory_scope_values() -> None:
    assert MemoryScope.USER.value == "user"
    assert MemoryScope.PROJECT.value == "project"


def test_memory_types() -> None:
    assert MEMORY_TYPES == frozenset({"user", "feedback", "project", "reference"})


def test_valid_name_accepts_kebab_case() -> None:
    assert valid_name("build-commands")
    assert valid_name("a")
    assert valid_name("user-prefs-2026")


@pytest.mark.parametrize(
    "name",
    ["Build Commands", "a/b", "..", "memory", "", "x" * 65, "with space", "Über"],
)
def test_valid_name_rejects_bad_names(name: str) -> None:
    assert not valid_name(name)


def test_validate_record_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="bad name", description="d", type="project", body="b"))
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="ok-name", description="d", type="wat", body="b"))
    with pytest.raises(ValueError):
        validate_record(MemoryRecord(name="ok-name", description="d", type="project", body="   "))


def test_file_content_round_trip() -> None:
    record = MemoryRecord(
        name="build-commands",
        description="How to build and test locally",
        type="project",
        body="Run pytest.",
    )
    text = file_content(record)
    assert text.startswith("---\n")
    assert "name: build-commands" in text
    assert "description: How to build and test locally" in text
    assert "type: project" in text
    assert text.rstrip().endswith("Run pytest.")

    restored = deserialize(text, MemoryScope.PROJECT)
    assert restored is not None
    assert restored.name == "build-commands"
    assert restored.description == "How to build and test locally"
    assert restored.type == "project"
    assert restored.body == "Run pytest."
    assert restored.scope is MemoryScope.PROJECT


def test_deserialize_returns_none_for_malformed() -> None:
    assert deserialize("no frontmatter here", MemoryScope.USER) is None
    assert deserialize("---\nnot: [valid yaml\n---\nbody", MemoryScope.USER) is None
    assert deserialize("---\n---\nbody", MemoryScope.USER) is None
    assert deserialize("---\ndescription: no name\n---\nbody", MemoryScope.USER) is None


def test_deserialize_defaults_missing_type_and_description() -> None:
    restored = deserialize("---\nname: solo\n---\nbody", MemoryScope.USER)
    assert restored is not None
    assert restored.type == "reference"
    assert restored.description == ""
