from tracker.repo_attribution import interaction_id, resolve_attribution


def test_interaction_id_deterministic() -> None:
    a = interaction_id("conv-1", "req-1", 1700000000000, "gpt-4", 0)
    b = interaction_id("conv-1", "req-1", 1700000000000, "gpt-4", 0)
    assert a == b
    assert len(a) == 64


def test_interaction_id_differs_on_change() -> None:
    a = interaction_id("conv-1", "req-1", 1700000000000, "gpt-4", 0)
    c = interaction_id("conv-1", "req-2", 1700000000000, "gpt-4", 0)
    assert a != c


def test_interaction_id_differs_on_turn_index() -> None:
    a = interaction_id("conv-1", "", 0, "", 0)
    b = interaction_id("conv-1", "", 0, "", 1)
    assert a != b


def test_manual_override_rules() -> None:
    rules = {
        "manual_overrides": {"abc": "my/repo"},
    }
    rk, _url, rid, conf = resolve_attribution("abc", "/tmp/x", "", "", rules)
    assert rk == "my/repo"
    assert rid == "manual_override"
    assert conf == 1.0


def test_resolve_empty_rules_uses_path() -> None:
    rk, _url, rid, _conf = resolve_attribution("x", "/tmp/myproject", "", "", {})
    assert rid == "heuristic_default"
    assert rk == "/tmp/myproject".lower().rstrip("/")
