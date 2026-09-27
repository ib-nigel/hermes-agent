"""The one-shot delegation budget refusal names the remaining allowance, not just 'exhausted'."""

from tools import delegate_tool


def _parent():
    return type("P", (), {})()


def test_charges_within_budget_then_refuses_over(monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(delegate_tool, "_get_oneshot_max_children", lambda: 3)
    parent = _parent()
    assert delegate_tool._oneshot_spawn_budget(parent, 1) is None
    assert delegate_tool._oneshot_spawn_budget(parent, 1) is None
    assert parent._oneshot_children_spawned == 2
    err = delegate_tool._oneshot_spawn_budget(parent, 2)
    assert err
    assert "allows at most 1 more subagent(s)" in err
    assert "(2/3 used" in err
    assert "delegation.oneshot_max_children" in err
    assert "inline" in err
    assert parent._oneshot_children_spawned == 2  # a refusal does not charge


def test_refusal_from_zero_spent_names_full_allowance(monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(delegate_tool, "_get_oneshot_max_children", lambda: 2)
    parent = _parent()
    err = delegate_tool._oneshot_spawn_budget(parent, 3)
    assert err and "allows at most 2 more subagent(s)" in err and "(0/2 used" in err


def test_unlimited_cap_never_charges(monkeypatch):
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setattr(delegate_tool, "_get_oneshot_max_children", lambda: 0)
    parent = _parent()
    assert delegate_tool._oneshot_spawn_budget(parent, 50) is None


def test_interactive_sessions_are_never_charged(monkeypatch):
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.setattr(delegate_tool, "_get_oneshot_max_children", lambda: 2)
    parent = _parent()
    assert delegate_tool._oneshot_spawn_budget(parent, 50) is None