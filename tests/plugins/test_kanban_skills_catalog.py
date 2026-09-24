"""Tests for the per-profile skills catalog and its kanban dashboard endpoint.

``hermes_cli.skills_catalog`` answers "which skills can this card's worker load?". A card's
``skills`` entries become one ``--skills <name>`` flag per entry on a worker spawned with
``-p <assignee>``, so the options are derived from each profile's own skills dir + that
profile's ``skills.disabled`` (a disabled skill is treated as missing by the preload path).

Coverage required by the task: a known profile, an unknown profile, and no profile param —
plus the disabled/installed-elsewhere edges the ``applicable`` flag exists for.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import skills_catalog

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_skill(skills_dir: Path, name: str, *, category: str | None = None,
                 description: str = "test skill") -> Path:
    """Write ``skills[/<category>]/<name>/SKILL.md`` (category omitted = uncategorized)."""
    skill_dir = skills_dir / category / name if category else skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8")
    return skill_dir


def _write_config(home: Path, *, disabled: list[str] | None = None) -> None:
    """Write ``home/config.yaml`` (also the named-profile identity marker)."""
    config: dict = {}
    if disabled:
        config["skills"] = {"disabled": disabled}
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


@pytest.fixture
def homes(_isolate_hermes_home, monkeypatch, tmp_path):
    """Isolated default home + two named profiles, each with its own skills and config.

    Profile enumeration also reads the *wrapper dir* on PATH
    (``profiles.build_alias_map``). On a machine whose wrappers live in the real
    ``~/.local/bin`` that read is outside the isolated home, so
    ``tests/home_io_guard`` refuses it and ``list_profiles()`` raises — the same
    thing that reddens ``tests/hermes_cli/test_gateway_multiplex_status.py`` on
    main. Point the wrapper dir at an empty tempdir: aliases are not part of the
    contract under test, profile names are.
    """
    from hermes_cli import profiles as profiles_mod

    empty_wrappers = tmp_path / "wrappers"
    empty_wrappers.mkdir()
    monkeypatch.setattr(profiles_mod, "_get_wrapper_dir", lambda: empty_wrappers)

    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    alpha = default_home / "profiles" / "worker_alpha"
    beta = default_home / "profiles" / "worker_beta"
    for home in (default_home, alpha, beta):
        (home / "skills").mkdir(parents=True, exist_ok=True)

    _write_skill(default_home / "skills", "shared-skill", category="devops")
    _write_skill(default_home / "skills", "default-skill", category="devops")
    _write_skill(default_home / "skills", "default-disabled", category="ops")
    _write_config(default_home, disabled=["default-disabled"])

    _write_skill(alpha / "skills", "shared-skill", category="devops")
    _write_skill(alpha / "skills", "alpha-skill", category="alpha")
    _write_config(alpha)

    _write_skill(beta / "skills", "shared-skill", category="devops")
    _write_skill(beta / "skills", "beta-skill", category="beta")
    _write_skill(beta / "skills", "beta-disabled", category="beta")
    _write_config(beta, disabled=["beta-disabled"])

    # Module-level caches outlive a test's HERMES_HOME.
    skills_catalog.clear_cache()
    import tools.skills_tool as skills_tool
    skills_tool._SKILLS_CACHE.clear()
    return {"default": default_home, "worker_alpha": alpha, "worker_beta": beta}


@pytest.fixture
def client(homes):
    """The plugin's router mounted on a bare app, as the dashboard mounts it."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_plugin_skills_catalog_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _ids(payload: dict) -> set[str]:
    return {s["id"] for s in payload["skills"]}


def _entry(payload: dict, skill_id: str) -> dict:
    return next(s for s in payload["skills"] if s["id"] == skill_id)


# ---------------------------------------------------------------------------
# Builder: known profile
# ---------------------------------------------------------------------------


def test_known_profile_returns_only_its_applicable_skills(homes):
    payload = skills_catalog.build_catalog("worker_alpha")

    assert payload["scope"] == "profile"
    assert payload["profile"] == "worker_alpha"
    assert payload["requested_profile"] == "worker_alpha"
    assert payload["resolved"] is True
    assert _ids(payload) == {"shared-skill", "alpha-skill"}
    assert all(s["applicable"] for s in payload["skills"])
    assert _entry(payload, "shared-skill")["profiles"] == ["worker_alpha"]


def test_known_profile_normalizes_name_and_shape(homes):
    payload = skills_catalog.build_catalog("Worker_Alpha")

    assert payload["profile"] == "worker_alpha"  # canonical id, not the raw param
    assert payload["requested_profile"] == "Worker_Alpha"
    entry = _entry(payload, "alpha-skill")
    assert set(entry) == {"id", "label", "category", "description", "applicable", "profiles"}
    assert entry["label"] == "alpha-skill"
    assert entry["category"] == "alpha"
    assert entry["description"] == "test skill"
    assert payload["count"] == len(payload["skills"])
    assert payload["profiles"] == ["default", "worker_alpha", "worker_beta"]


def test_disabled_skill_is_not_offered_for_its_profile(homes):
    """A disabled skill never reaches a worker (preload treats it as missing)."""
    assert "default-disabled" not in _ids(skills_catalog.build_catalog("default"))
    assert "beta-disabled" not in _ids(skills_catalog.build_catalog("worker_beta"))


def test_profile_skills_are_sorted_by_category_then_id(homes):
    payload = skills_catalog.build_catalog("worker_alpha")
    assert [(s["category"], s["id"]) for s in payload["skills"]] == [
        ("alpha", "alpha-skill"), ("devops", "shared-skill")]


def test_current_alias_resolves_to_this_process_profile(homes):
    payload = skills_catalog.build_catalog("current")
    assert payload["scope"] == "profile"
    assert payload["profile"] == "default"  # the test process's own profile


# ---------------------------------------------------------------------------
# Builder: unknown / missing profile -> full catalog, never an error
# ---------------------------------------------------------------------------


def test_unknown_profile_falls_back_to_full_catalog(homes):
    payload = skills_catalog.build_catalog("no-such-profile")

    assert payload["scope"] == "catalog"
    assert payload["profile"] is None
    assert payload["requested_profile"] == "no-such-profile"
    assert payload["resolved"] is False  # the UI can warn without an empty picker
    assert _ids(payload) == {
        "shared-skill", "default-skill", "default-disabled", "alpha-skill", "beta-skill", "beta-disabled"}


def test_missing_profile_returns_full_catalog(homes):
    payload = skills_catalog.build_catalog()
    assert payload["scope"] == "catalog"
    assert payload["requested_profile"] is None
    assert payload["resolved"] is True
    assert _ids(payload) == _ids(skills_catalog.build_catalog("no-such-profile"))


def test_blank_profile_param_is_treated_as_no_profile(homes):
    """``?profile=`` with an empty value is the same request as omitting it."""
    for raw in ("", "   "):
        payload = skills_catalog.build_catalog(raw)
        assert payload["scope"] == "catalog"
        assert payload["requested_profile"] is None
        assert payload["resolved"] is True
        assert payload["skills"]


def test_malformed_profile_names_do_not_error(homes):
    """A name that cannot be a profile id degrades to the catalog instead of raising."""
    for raw in ("../escape", "not a profile", "UPPER--bad--"):
        payload = skills_catalog.build_catalog(raw)
        assert payload["scope"] == "catalog"
        assert payload["resolved"] is False
        assert payload["skills"]


def test_catalog_flags_where_each_skill_applies(homes):
    payload = skills_catalog.build_catalog()

    assert _entry(payload, "shared-skill")["profiles"] == ["default", "worker_alpha", "worker_beta"]
    assert _entry(payload, "shared-skill")["applicable"] is True
    assert _entry(payload, "alpha-skill")["profiles"] == ["worker_alpha"]
    # Installed, but disabled in the only profile that has it -> not applicable anywhere.
    assert _entry(payload, "beta-disabled")["applicable"] is False
    assert _entry(payload, "beta-disabled")["profiles"] == []


# ---------------------------------------------------------------------------
# Endpoint: GET /api/plugins/kanban/skills
# ---------------------------------------------------------------------------


def test_endpoint_known_profile(client):
    resp = client.get("/api/plugins/kanban/skills", params={"profile": "worker_alpha"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "profile"
    assert _ids(body) == {"shared-skill", "alpha-skill"}


def test_endpoint_unknown_profile_is_not_an_error(client):
    resp = client.get("/api/plugins/kanban/skills", params={"profile": "ghost"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is False
    assert body["scope"] == "catalog"
    assert "default-skill" in _ids(body)


def test_endpoint_without_profile_returns_full_catalog(client):
    resp = client.get("/api/plugins/kanban/skills")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "catalog"
    assert body["profile"] is None
    assert _ids(body) == _ids(skills_catalog.build_catalog())


def test_envelope_is_not_served_from_another_requests_cache(client):
    """Regression: the cached skill rows must not carry ``resolved``/``requested_profile``.

    A no-profile request used to warm a catalog-scope cache entry that then answered a later
    ``?profile=ghost`` request with ``resolved: true`` — the picker would show no warning.
    """
    assert client.get("/api/plugins/kanban/skills").json()["resolved"] is True

    ghost = client.get("/api/plugins/kanban/skills", params={"profile": "ghost"}).json()
    assert ghost["resolved"] is False
    assert ghost["requested_profile"] == "ghost"
    assert _ids(ghost) == _ids(client.get("/api/plugins/kanban/skills").json())


def test_profile_envelope_echoes_the_requested_spelling(client):
    """Same class of bug on the profile scope: the raw param is echoed, not the cached one."""
    first = client.get("/api/plugins/kanban/skills", params={"profile": "Worker_Alpha"}).json()
    second = client.get("/api/plugins/kanban/skills", params={"profile": "worker_alpha"}).json()

    assert first["requested_profile"] == "Worker_Alpha"
    assert second["requested_profile"] == "worker_alpha"
    assert first["profile"] == second["profile"] == "worker_alpha"
    assert _ids(first) == _ids(second)


def test_cached_rows_cannot_be_mutated_by_a_caller(client):
    """The response is a copy — a client poking at it must not corrupt the next response."""
    first = skills_catalog.build_catalog("worker_alpha")
    first["skills"].clear()
    first["skills"].append({"id": "injected", "profiles": []})

    second = skills_catalog.build_catalog("worker_alpha")
    assert "injected" not in _ids(second)
    assert _ids(second) == {"shared-skill", "alpha-skill"}
