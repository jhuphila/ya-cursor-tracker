from pathlib import Path

from tracker.cursor_sources import path_from_uri_obj


def test_path_from_uri_dict_fsPath() -> None:
    p = path_from_uri_obj({"fsPath": "C:\\tmp\\proj\\src\\a.py", "scheme": "file"})
    assert p is not None
    assert "proj" in str(p)


def test_path_from_uri_external() -> None:
    p = path_from_uri_obj({"external": "file:///C:/tmp/foo"})
    assert p is not None
