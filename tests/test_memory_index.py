from lanscoder.memory.index import MemoryIndex
from lanscoder.memory.models import MemoryRecord, MemoryScope


def test_render_empty() -> None:
    assert MemoryIndex.render([]) == ""


def test_render_one_line_per_record() -> None:
    records = [
        MemoryRecord(name="build-commands", description="How to build", type="project", body="x", scope=MemoryScope.PROJECT),
        MemoryRecord(name="user-prefs", description="Prefers Chinese", type="user", body="y", scope=MemoryScope.USER),
    ]
    assert MemoryIndex.render(records) == ("- [build-commands](build-commands.md) — How to build\n" "- [user-prefs](user-prefs.md) — Prefers Chinese")
