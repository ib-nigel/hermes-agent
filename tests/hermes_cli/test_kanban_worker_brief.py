"""The dispatched worker prompt: stub first line, board brief after it.

Measured motivation: workers were spawned with nothing but ``work kanban task <id>``
(27 characters), so each one spent its first ~25 tool calls re-deriving the board
row, the repo layout and the constraints. The brief hands over what the dispatcher
has already read. The stub line must not move: the e2e worker-contract regex
(``tests/e2e/core/kanban/test_kanban_worker_contract.py``), the session-attribution
reader that pairs a board task with the sessions that worked it, and the auto-titler
all key on it.
"""

from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

_DEFAULT_BODY = "Rotate it in Secrets Manager, then re-run the smoke suite."


def _task(
    *,
    body: str | None = _DEFAULT_BODY,
    completion_contract: str | None = None,
    consecutive_failures: int = 0,
    last_failure_error: str | None = None,
    branch_name: str | None = None,
    workspace_path: str | None = None,
) -> kb.Task:
    return kb.Task(
        id="t_brief0001",
        title="Rotate the staging AWS key",
        body=body,
        assignee="injury-bridge",
        status="todo",
        priority=2,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=workspace_path,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        current_run_id=None,
        consecutive_failures=consecutive_failures,
        last_failure_error=last_failure_error,
        branch_name=branch_name,
        completion_contract=completion_contract,
    )


def test_spawned_argv_keeps_the_stub_as_the_first_line(monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _h: None)

    cmd = kbd._worker_argv(_task(), "injury-bridge", None)
    query = cmd[cmd.index("-q") + 1]

    assert query.splitlines()[0] == "work kanban task t_brief0001"
    assert query.startswith(kbd.WORKER_PROMPT_STUB.format(task_id="t_brief0001"))
    assert "Title: Rotate the staging AWS key" in query
    assert "Rotate it in Secrets Manager" in query


def test_brief_carries_what_a_worker_would_otherwise_look_up():
    q = kbd._worker_brief(_task(
        completion_contract="Staging login succeeds with the rotated key",
        consecutive_failures=1,
        last_failure_error="exit 137",
        branch_name="wt/t_brief0001",
        workspace_path="/tmp/wt",
    ))

    assert "Done means: Staging login succeeds with the rotated key" in q
    assert "Retry 1" in q and "exit 137" in q
    assert "Branch: wt/t_brief0001" in q
    assert "Workspace: dir /tmp/wt" in q


def test_missing_body_omits_the_section_but_still_briefs():
    q = kbd._worker_brief(_task(body=None))

    assert "Title: Rotate the staging AWS key" in q
    assert "\nBody:" not in q


def test_oversized_body_is_clipped_not_passed_through():
    q = kbd._worker_brief(_task(body="x" * (kbd.WORKER_BRIEF_BODY_LIMIT + 500)))

    assert "more characters on the board" in q
    assert len(q) < kbd.WORKER_BRIEF_BODY_LIMIT + 600


def test_none_task_contributes_nothing():
    assert kbd._worker_brief(None) == ""


def test_a_workspace_named_for_another_task_is_flagged_not_hidden():
    """Batched creation has pointed many rows at one scratch dir; a worker told
    'Workspace: <some other task's dir>' with no warning edits the wrong checkout."""
    q = kbd._worker_brief(_task(workspace_path="/tmp/kanban/workspaces/t_6873a29f"))
    assert "named for t_6873a29f, not this task" in q

    own = kbd._worker_brief(_task(workspace_path="/tmp/kanban/workspaces/t_brief0001"))
    assert "Named for" not in own and "named for" not in own
