"""Interim commentary preview state tests."""

from hermes_lark_streaming.streaming.preview import InterimPreviewState


def test_replace_is_replace_only_and_revision_safe() -> None:
    state = InterimPreviewState()

    assert state.replace("Checking repo") is True
    assert state.text == "Checking repo"
    assert state.revision == 1
    assert state.dirty is True
    assert state.replace("Found handler") is True
    assert state.text == "Found handler"
    assert state.revision == 2
    assert state.replace("Found handler") is False

    assert state.mark_rendered(1) is False
    assert state.dirty is True
    assert state.mark_rendered(2) is True
    assert state.dirty is False


def test_start_final_clears_rendered_preview_and_blocks_late_commentary() -> None:
    state = InterimPreviewState()
    state.replace("Checking")
    state.created = True
    state.mark_rendered(state.revision)

    assert state.start_final() is True
    assert state.final_started is True
    assert state.text == ""
    assert state.dirty is True
    assert state.replace("Late commentary") is False
    assert state.start_final() is False


def test_reset_render_state_recreates_only_live_preview() -> None:
    state = InterimPreviewState()
    state.replace("Latest")
    state.created = True
    state.mark_rendered(state.revision)

    state.reset_render_state()

    assert state.created is False
    assert state.text == "Latest"
    assert state.dirty is True

    state.start_final()
    state.reset_render_state()
    assert state.dirty is False
