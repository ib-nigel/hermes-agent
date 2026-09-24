"""Card ``skills``: the comma-separated string -> JSON array migration.

A card used to persist its force-loaded skills as one comma-separated string;
the canonical column shape is now a JSON array of skill ids. These tests pin
the three halves of that change:

* the one-shot migration in ``kanban_db_connect`` rewrites legacy values
  (split, trim, de-duplicate, drop blanks) without inventing or losing skills,
* a read tolerates a legacy value even before the rewrite has run,
* the write paths (``create_task`` / ``edit_task``) persist an array and still
  accept the legacy string, so an older client does not crash the board.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _raw_skills(conn, task_id) -> str | None:
    """The bytes on disk — the point of this whole change."""
    return conn.execute("SELECT skills FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


def _seed_task(conn, *, skills=None) -> str:
    return kb.create_task(conn, title="legacy card", assignee="default", skills=skills)


def _legacy_write(conn, task_id, value: str | None) -> None:
    """Recreate the pre-migration column contents for one row."""
    conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (value, task_id))
    conn.commit()


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_splits_legacy_strings_and_keeps_arrays_untouched(kanban_home):
    conn = kbc.connect()
    try:
        legacy = _seed_task(conn, skills=["placeholder"])
        messy = _seed_task(conn, skills=["placeholder"])
        blanks = _seed_task(conn, skills=["placeholder"])
        separators_only = _seed_task(conn, skills=["placeholder"])
        already_array = _seed_task(conn, skills=["a", "b"])
        unset = _seed_task(conn)

        _legacy_write(conn, legacy, "hermes-agent,test-driven-development")
        _legacy_write(conn, messy, " a ,,a ,  b ,")
        _legacy_write(conn, blanks, "")
        _legacy_write(conn, separators_only, ",,")
        before_array = _raw_skills(conn, already_array)

        rewritten = kbc._migrate_legacy_skills_column(conn)

        # Exactly the four non-array values with content were rewritten; the
        # canonical array and the NULL row are left alone.
        assert rewritten == 4
        assert json.loads(_raw_skills(conn, legacy)) == ["hermes-agent", "test-driven-development"]
        # Trimmed, de-duplicated, blanks dropped — in first-seen order.
        assert json.loads(_raw_skills(conn, messy)) == ["a", "b"]
        # A value with no names left is "not set", not an empty-string entry.
        assert _raw_skills(conn, blanks) is None
        assert _raw_skills(conn, separators_only) is None
        assert _raw_skills(conn, already_array) == before_array
        assert _raw_skills(conn, unset) is None
    finally:
        conn.close()


def test_migration_is_idempotent(kanban_home):
    conn = kbc.connect()
    try:
        tid = _seed_task(conn, skills=["placeholder"])
        _legacy_write(conn, tid, "one, two")
        assert kbc._migrate_legacy_skills_column(conn) == 1
        # Second pass: nothing left to rewrite, and the value is stable.
        assert kbc._migrate_legacy_skills_column(conn) == 0
        assert json.loads(_raw_skills(conn, tid)) == ["one", "two"]
        assert kbc._migrate_legacy_skills_column(conn) == 0
    finally:
        conn.close()


def test_init_db_runs_the_skills_migration(kanban_home):
    """The rewrite is wired into the migration pass ``connect()``/``init_db``
    runs, not just callable by hand."""
    conn = kbc.connect()
    try:
        tid = _seed_task(conn, skills=["placeholder"])
        _legacy_write(conn, tid, "hermes-agent , hermes-agent")
    finally:
        conn.close()

    kb.init_db()

    with kbc.connect_closing() as conn:
        assert json.loads(_raw_skills(conn, tid)) == ["hermes-agent"]
        # ...and the row now reads back through the model as an array.
        assert kb.get_task(conn, tid).skills == ["hermes-agent"]


def test_read_tolerates_a_legacy_value_before_migration(kanban_home):
    """A read must not drop skills just because the rewrite hasn't run yet."""
    conn = kbc.connect()
    try:
        tid = _seed_task(conn, skills=["placeholder"])
        _legacy_write(conn, tid, "alpha, beta")
        assert kb.get_task(conn, tid).skills == ["alpha", "beta"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def test_create_stores_an_array_for_both_input_shapes(kanban_home):
    conn = kbc.connect()
    try:
        as_list = _seed_task(conn, skills=["labfinder-verify-cli", "test-driven-development"])
        assert _raw_skills(conn, as_list) == '["labfinder-verify-cli", "test-driven-development"]'
        assert kb.get_task(conn, as_list).skills == ["labfinder-verify-cli", "test-driven-development"]

        # Legacy comma-separated string from an older client: split, not stored
        # as one name.
        as_string = _seed_task(conn, skills="labfinder-verify-cli, test-driven-development")
        assert kb.get_task(conn, as_string).skills == ["labfinder-verify-cli", "test-driven-development"]

        # Duplicates and blanks never reach the column.
        deduped = _seed_task(conn, skills=["a", " a ", "", "b", "a"])
        assert json.loads(_raw_skills(conn, deduped)) == ["a", "b"]

        # A comma-joined string that arrived as a list element (what the CLI
        # does with `--skill "a, b"`) is split too: one argv slot must never
        # carry several names to the worker, and the shape must not silently
        # become a single bogus skill.
        joined = _seed_task(conn, skills=["labfinder-angular-app, labfinder-verify-cli, labfinder-angular-app"])
        assert json.loads(_raw_skills(conn, joined)) == ["labfinder-angular-app", "labfinder-verify-cli"]

        none = _seed_task(conn, skills=[])
        assert _raw_skills(conn, none) == "[]"
        assert kb.get_task(conn, none).skills == []

        unset = _seed_task(conn)
        assert _raw_skills(conn, unset) is None
        assert kb.get_task(conn, unset).skills is None
    finally:
        conn.close()


def test_edit_replaces_skills_and_leaves_them_alone_when_omitted(kanban_home):
    conn = kbc.connect()
    try:
        tid = _seed_task(conn, skills=["keep-me"])

        assert kb.edit_task(conn, tid, title="renamed")
        assert kb.get_task(conn, tid).skills == ["keep-me"]

        assert kb.edit_task(conn, tid, skills=["labfinder-angular-app", "labfinder-angular-app"])
        assert _raw_skills(conn, tid) == '["labfinder-angular-app"]'
        assert kb.get_task(conn, tid).skills == ["labfinder-angular-app"]

        # Legacy string through the edit path too.
        assert kb.edit_task(conn, tid, skills="a, b")
        assert kb.get_task(conn, tid).skills == ["a", "b"]

        # Explicit clear survives as [], distinct from "not set".
        assert kb.edit_task(conn, tid, skills=[])
        assert _raw_skills(conn, tid) == "[]"
        assert kb.get_task(conn, tid).skills == []
    finally:
        conn.close()


def test_edit_records_skills_in_the_edited_event(kanban_home):
    conn = kbc.connect()
    try:
        tid = _seed_task(conn, skills=["old"])
        assert kb.edit_task(conn, tid, skills=["new"])
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'edited'", (tid,)
        ).fetchall()
        payloads = [json.loads(e["payload"]) for e in events if e["payload"]]
        assert {"fields": ["skills"]} in payloads
    finally:
        conn.close()


def test_toolset_names_are_still_refused_on_the_write_path(kanban_home):
    """The write path keeps its guard: a toolset is not a skill. It also splits
    a comma-joined string first, so the refusal fires on the real names."""
    conn = kbc.connect()
    try:
        with pytest.raises(ValueError, match="toolset"):
            _seed_task(conn, skills=["web"])
        with pytest.raises(ValueError, match="toolset"):
            _seed_task(conn, skills="web, browser")
        assert kb.get_task(conn, _seed_task(conn)) is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI `hermes kanban edit --skill`
# ---------------------------------------------------------------------------


def _edit_args(task_id, *extra):
    import argparse

    from hermes_cli import kanban_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban_parser.build_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["kanban", "edit", task_id, *extra])


def test_cli_edit_sets_skills_and_keeps_them_without_the_flag(kanban_home, capsys):
    from hermes_cli import kanban as kb_cli

    with kbc.connect_closing() as conn:
        tid = _seed_task(conn, skills=["old-skill"])

    assert kb_cli._cmd_edit(_edit_args(tid, "--skill", "a, b", "--skill", "c")) == 0
    capsys.readouterr()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).skills == ["a", "b", "c"]

    # An edit that doesn't pass --skill leaves them alone.
    assert kb_cli._cmd_edit(_edit_args(tid, "--title", "renamed")) == 0
    capsys.readouterr()
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.title == "renamed"
        assert task.skills == ["a", "b", "c"]


def test_cli_edit_without_fields_is_a_usage_error(kanban_home, capsys):
    from hermes_cli import kanban as kb_cli

    with kbc.connect_closing() as conn:
        tid = _seed_task(conn, skills=["keep"])

    assert kb_cli._cmd_edit(_edit_args(tid)) == 2
    assert "--skill" in capsys.readouterr().err


def test_normalize_skills_shapes():
    from hermes_cli.kanban_db import normalize_skills

    assert normalize_skills(None) is None
    assert normalize_skills("") is None
    assert normalize_skills("   ") is None
    assert normalize_skills([]) == []
    assert normalize_skills("a, b") == ["a", "b"]
    assert normalize_skills(["a", "a", " b ", None, ""]) == ["a", "b"]
    # A list element that is itself comma-joined (CLI `--skill "a, b"`).
    assert normalize_skills(["a, b", "b, c"]) == ["a", "b", "c"]
    assert normalize_skills(42) == ["42"]