from tracker.cursor_sources import _composer_display_title


def test_composer_title_prefers_name() -> None:
    assert _composer_display_title({"name": " My session ", "title": "ignored"}) == "My session"


def test_composer_title_fallback_title() -> None:
    assert _composer_display_title({"title": "Fallback"}) == "Fallback"


def test_composer_title_empty() -> None:
    assert _composer_display_title({}) == ""
