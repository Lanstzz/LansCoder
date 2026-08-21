from lanscoder.app.tui_state import BlockKind, ChildKind, ChildItem, TranscriptBlock, TranscriptModel  # noqa: F401


def test_model_collects_blocks_in_order():
    model = TranscriptModel()
    model.add_block(BlockKind.USER, "hi")
    model.add_block(BlockKind.ASSISTANT)
    model.blocks[1].children.append(ChildItem(ChildKind.TOOL, "c1", "tool read", status="running"))
    assert [b.kind for b in model.blocks] == [BlockKind.USER, BlockKind.ASSISTANT]
    assert model.blocks[1].children[0].status == "running"


def test_model_last_block_tracks_head():
    model = TranscriptModel()
    assert model.last_block() is None
    model.add_block(BlockKind.SYSTEM, "note")
    assert model.last_block().text == "note"


def test_model_find_last_command_block_skips_newer_blocks():
    model = TranscriptModel()
    model.add_block(BlockKind.COMMAND, "first")
    model.add_block(BlockKind.SYSTEM, "note")
    found = model.find_last_command_block()
    assert found is not None and found.text == "first"


def test_model_clear_resets():
    model = TranscriptModel()
    model.add_block(BlockKind.USER, "hi")
    model.clear()
    assert model.blocks == []