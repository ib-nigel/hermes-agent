"""Profile-aware skills multi-select: bundle contracts + REST round-trip.

The kanban card editor used to take skills as a free-text comma-separated
string, which meant an operator had to *know* which skills the selected
assignee's worker can actually force-load. The picker now loads its options
from ``GET /api/plugins/kanban/skills?profile=<assignee>`` (built by
``hermes_cli/skills_catalog.py``) and submits the selection as the array the
card stores.

The bundle (``plugins/kanban/dashboard/dist/index.js``) has no build step and
no test harness of its own, so — as with the rest of this plugin's UI coverage
— the UI seams are pinned by asserting on the shipped source, while the REST
shapes it depends on are exercised behaviourally through the plugin router.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_PATH = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    plugin_file = REPO_ROOT / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_skills_ui_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture(scope="module")
def bundle() -> str:
    return BUNDLE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Bundle: the comma-separated field is gone, the picker is in both editors
# ---------------------------------------------------------------------------


def test_bundle_has_no_comma_separated_skills_field(bundle):
    """The free-text field (and its placeholder/hint copy) must be gone, and
    the create dialog must submit the array the card stores."""
    assert "comma-separated" not in bundle
    assert "skillsPlaceholder" not in bundle
    assert ".split(\",\")" not in bundle
    # InlineCreate submits the picker's selection as-is (array of ids).
    assert "if (skills.length > 0) body.skills = skills.slice();" in bundle
    # …and resets it to an empty array between opens.
    assert 'setParent(""); setSkills([]);' in bundle


def test_picker_is_used_by_both_the_create_dialog_and_the_drawer(bundle):
    """One control, two surfaces: the create dialog and the card drawer's
    editable Skills row (where the assignee — i.e. the profile — changes)."""
    assert "function SkillsPicker(props)" in bundle
    assert "function SkillsEditor(props)" in bundle
    assert "h(SkillsEditor, { task: t, onPatch: props.onPatch })," in bundle
    # Create dialog wires the typed assignee in, debounced (a text field would
    # otherwise fire one catalog request per keystroke).
    assert "profile: assignee.trim()," in bundle
    assert "debounceMs: 400," in bundle


def test_picker_options_come_from_the_profile_scoped_catalog_api(bundle):
    assert "const url = key" in bundle
    assert "`${API}/skills?profile=${encodeURIComponent(key)}`" in bundle
    assert ": `${API}/skills`;" in bundle
    # Rows/cache: a failed fetch is not cached, so Retry can succeed.
    assert "if (skillsCache[key] === entry) delete skillsCache[key];" in bundle
    assert "onClick: catalog.reload," in bundle


# ---------------------------------------------------------------------------
# Bundle: profile changes reconcile, never silently drop
# ---------------------------------------------------------------------------


def test_profile_change_reconciles_and_flags_stale_selections(bundle):
    assert "function reconcileSkills(selected, data)" in bundle
    # A missing id is KEPT: it renders as a chip flagged as unavailable, with
    # an explicit warning line naming it.
    assert "hermes-kanban-skill-chip--missing" in bundle
    assert 'tx(t, "skillsMissingWarn",' in bundle
    assert "Kept on the card and flagged" in bundle
    # Stale state guard: a render between the profile change and the
    # (debounced) fetch reports loading, so the OTHER profile's list can never
    # be shown or flagged against.
    assert "const fresh = state.profile === wanted;" in bundle
    assert 'status: fresh ? state.status : "loading",' in bundle


def test_picker_covers_loading_empty_error_and_unresolved_profile(bundle):
    # Loading + error states for the options fetch itself.
    assert 'tx(t, "skillsLoadingRows", "Loading skills…")' in bundle
    assert 'tx(t, "skillsLoadFailed", "Could not load skills")' in bundle
    assert 'className: "hermes-kanban-skill-error",' in bundle
    # Empty states: no applicable skills vs. no search match vs. truncated.
    assert "skillsNoneForProfile" in bundle
    assert "skillsNoMatch" in bundle
    assert "skillsTruncated" in bundle
    # Unknown profile is a WARNING, not an error and not an empty control.
    assert "catalog.data.resolved === false" in bundle
    assert "hermes-kanban-skill-scope--warn" in bundle
    assert "showing the full catalog" in bundle


def test_rejected_skills_patch_is_surfaced_in_the_drawer(bundle):
    """A 400 from the skills PATCH carries its reason in ``detail``; the drawer
    must render it instead of letting the save look like a no-op."""
    assert 'setPatchErr(parseApiErrorMessage(e));' in bundle
    assert 'patchErr ? h("div", {' in bundle
    assert '"data-testid": "kanban-patch-error",' in bundle


# ---------------------------------------------------------------------------
# REST: the array round-trips, and a rejected value is a 400 with a reason
# ---------------------------------------------------------------------------


def test_skills_patch_and_create_round_trip_as_arrays(client):
    created = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "picker card", "skills": ["test-driven-development", "systematic-debugging"]},
    )
    assert created.status_code == 200, created.text
    task = created.json()["task"]
    assert task["skills"] == ["test-driven-development", "systematic-debugging"]
    task_id = task["id"]

    # Reload (what the drawer does) — still an array, same order.
    fetched = client.get(f"/api/plugins/kanban/tasks/{task_id}").json()["task"]
    assert fetched["skills"] == ["test-driven-development", "systematic-debugging"]

    # PATCH replaces the list; [] is an explicit clear.
    patched = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}", json={"skills": ["systematic-debugging"]},
    )
    assert patched.status_code == 200, patched.text
    assert client.get(f"/api/plugins/kanban/tasks/{task_id}").json()["task"]["skills"] == [
        "systematic-debugging"
    ]

    cleared = client.patch(f"/api/plugins/kanban/tasks/{task_id}", json={"skills": []})
    assert cleared.status_code == 200, cleared.text
    assert client.get(f"/api/plugins/kanban/tasks/{task_id}").json()["task"]["skills"] == []


def test_rejected_skill_value_returns_400_with_reason(client):
    """The picker offers skill ids only; a toolset name is refused with the
    reason in ``detail`` (the drawer banner shows exactly that string)."""
    task = client.post("/api/plugins/kanban/tasks", json={"title": "bad skill"}).json()["task"]
    resp = client.patch(f"/api/plugins/kanban/tasks/{task['id']}", json={"skills": "web"})
    assert resp.status_code == 400, resp.text
    assert resp.json().get("detail")


def test_skills_catalog_marks_unknown_profile_unresolved(client):
    """The picker renders a warning from ``resolved``/``profile`` — never an
    empty control — when the typed assignee is not an installed profile."""
    ok = client.get("/api/plugins/kanban/skills", params={"profile": "default"}).json()
    assert ok["resolved"] is True
    assert ok["requested_profile"] == "default"

    unknown = client.get("/api/plugins/kanban/skills", params={"profile": "nope-not-real"})
    assert unknown.status_code == 200, unknown.text
    body = unknown.json()
    assert body["resolved"] is False
    assert body["requested_profile"] == "nope-not-real"
    assert body["scope"] == "catalog"
    # Shape, not size: this fixture's HERMES_HOME has no skills at all, but the
    # contract the picker renders from (a non-error catalog envelope) holds.
    assert isinstance(body["skills"], list)
    assert body["count"] == len(body["skills"])
