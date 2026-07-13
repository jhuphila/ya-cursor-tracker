from tracker.cursor_sources import (
    WorkspaceConvMap,
    _composer_display_title,
    apply_workspace_map_to_attribution,
)


def test_composer_title_prefers_name() -> None:
    assert _composer_display_title({"name": " My session ", "title": "ignored"}) == "My session"


def test_composer_title_fallback_title() -> None:
    assert _composer_display_title({"title": "Fallback"}) == "Fallback"


def test_composer_title_empty() -> None:
    assert _composer_display_title({}) == ""


def test_apply_workspace_map_overrides_unattributed() -> None:
    ws = WorkspaceConvMap(
        conv_to_root={"a": "/repos/a", "b": "/repos/b"},
        conv_to_workspace_id={},
        conv_to_title={"a": "Session A"},
        conv_to_attribution_source={"a": "global-composer-data", "b": "global-composer-headers"},
    )
    attr = {
        "a": {"repo_path": "__unattributed__", "layer": "commit-linked"},
        "c": {"repo_path": "/keep/me", "layer": "commit-linked"},
    }
    apply_workspace_map_to_attribution(attr, ws)
    assert attr["a"] == {"repo_path": "/repos/a", "layer": "global-composer-data"}
    assert attr["b"] == {"repo_path": "/repos/b", "layer": "global-composer-headers"}
    assert attr["c"] == {"repo_path": "/keep/me", "layer": "commit-linked"}
