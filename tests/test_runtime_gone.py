from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NEEDLE = "lanscoder." + "runtime"


def test_no_lanscoder_runtime_references():
    hits = []
    for base in (REPO / "lanscoder", REPO / "tests"):
        for path in base.rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if NEEDLE in text:
                hits.append(str(path.relative_to(REPO)))
    assert hits == []
