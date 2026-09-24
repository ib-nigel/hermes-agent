"""Per-profile skills catalog — the source of truth for a card editor's skills picker.

WHY THIS EXISTS
    A kanban card's ``skills`` list is force-loaded into its worker as one ``--skills <name>``
    flag per entry, and that worker runs under ``-p <assignee>`` with the assignee profile's
    HERMES_HOME. So "which skills can this card load?" is a PER-PROFILE question, and the
    answer is derived from profile metadata (the profile's skills dirs + that profile's own
    ``skills.disabled``) rather than a hand-maintained map: profiles are created and removed
    at runtime (``hermes profile create`` / ``profile delete``), so a static map would go
    stale on the first install. Applicability is "the identifier resolves for that profile
    AND is not disabled there" — ``agent.skill_commands.build_preloaded_skills_prompt``
    treats a disabled skill as missing, so a disabled skill never reaches the worker.

    Profile resolution reuses the dashboard's own profile scope
    (``hermes_cli.web_server_profiles._profile_scope``), the same seam
    ``GET /api/skills?profile=`` uses, so this catalog and the Skills page cannot disagree
    about what is installed for a profile. The enumeration is the canonical
    ``tools.skills_tool._find_all_skills`` one, so the platform/environment gates that hide a
    skill from ``skills_list``/the Skills page also hide it here (``--skills`` would still
    force-load such a name — it bypasses the scan-time gate — but offering a skill the profile
    never advertises is not a useful option).

RESPONSE SHAPE (stable contract; consumed by the kanban card editor)
    ``GET /api/plugins/kanban/skills[?profile=<name>]`` ->
    ``{"scope": "profile"|"catalog", "profile": str|None, "requested_profile": str|None,
       "resolved": bool, "profiles": [str], "count": int,
       "skills": [{"id": str, "label": str, "category": str|None, "description": str,
                   "applicable": bool, "profiles": [str]}]}``

    * ``id``      — the value to store on the card and pass to ``--skills``.
    * ``label``   — display label (currently identical to ``id``; kept separate so a prettier
                    display name can be added later without a contract change).
    * ``applicable`` — installed for a profile and enabled there.
    * ``profiles``   — the profiles where the entry is applicable.

SCOPES
    * ``?profile=<known>``   -> ``scope: "profile"``: that profile's APPLICABLE skills only
                                (every entry ``applicable: true``), sorted by category then id.
    * ``?profile=<unknown>`` -> HTTP 200, ``scope: "catalog"``, ``resolved: false``. Documented
                                default: a renamed/removed profile must not empty the picker,
                                so callers get the full catalog and can warn from ``resolved``.
    * no ``profile``         -> ``scope: "catalog"``, ``resolved: true``: every skill installed
                                in ANY known profile, each flagged with where it applies
                                (``applicable: false`` = installed somewhere, disabled everywhere).

    Never raises for a bad profile name: an unknown/blank/malformed one degrades to the
    catalog scope. Results are cached briefly (``CACHE_TTL_SECONDS``) because the catalog
    scope walks every profile's skills tree.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

# Skills change on the order of a skill install, not per keystroke; the picker may lag this
# long after an install. Matches tools.skills_tool's own scan TTL.
CACHE_TTL_SECONDS = 30.0

_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}

# Scope alias meaning "this process's own profile" (see web_server_profiles._is_current_profile).
# A MISSING param is deliberately not an alias: it means the full catalog.
_CURRENT_ALIASES = frozenset({"current"})


# --- Profile enumeration -----------------------------------------------------

def known_profiles() -> list[str]:
    """Canonical names of every live profile (``default`` first, then alphabetical)."""
    try:
        from hermes_cli import profiles as profiles_mod
        names = [p.name for p in profiles_mod.list_profiles(lazy_skill_count=True)]
    except Exception:
        return ["default"]
    return names or ["default"]


def resolve_profile(name: Optional[str]) -> Optional[str]:
    """Canonical profile name for *name*, or ``None`` when it is not a live profile.

    ``current`` resolves to this process's own profile name (mirroring ``_profile_scope``'s
    alias); a blank, unknown, or malformed name is simply unresolved — never an error.
    """
    raw = (name or "").strip()
    try:
        from hermes_cli import profiles as profiles_mod
        if raw.casefold() in _CURRENT_ALIASES:
            return profiles_mod.get_active_profile_name() or "default"
        if not profiles_mod.profile_exists(raw):
            return None
        return profiles_mod.normalize_profile_name(raw)
    except Exception:
        return None


# --- Skill enumeration -------------------------------------------------------

def _scoped_skills(profile: Optional[str]) -> list[dict[str, Any]]:
    """Every skill INSTALLED for *profile*, each with an ``applicable`` flag.

    Runs inside the dashboard's profile scope so ``_find_all_skills`` resolves that profile's
    skills dirs and ``load_config`` reads that profile's ``skills.disabled``.
    """
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills
    from hermes_cli.web_server_profiles import _profile_scope
    from tools.skills_tool import _find_all_skills

    with _profile_scope(profile):
        disabled = get_disabled_skills(load_config() or {})
        found = _find_all_skills(skip_disabled=True)

    rows: list[dict[str, Any]] = []
    for skill in found:
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        rows.append({
            "id": name,
            "label": name,
            "category": skill.get("category") or None,
            "description": skill.get("description") or "",
            "applicable": name not in disabled,
            "profiles": [],
        })
    return rows


def _sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable order for every listing path: category, then id."""
    return sorted(rows, key=lambda r: (r.get("category") or "", r["id"]))


# --- Cache -------------------------------------------------------------------
# Only the SKILL ROWS are memoized (the expensive part: one skills-tree walk per profile).
# The response envelope is rebuilt per call, because ``requested_profile``/``resolved`` depend
# on the raw query param: caching the envelope would let a no-profile request's payload answer
# a later ``?profile=ghost`` request with ``resolved: true``.

def _cached_rows(key: tuple, build) -> list[dict[str, Any]]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and (now - hit[0]) < CACHE_TTL_SECONDS:
            return hit[1]
    rows = build()
    with _CACHE_LOCK:
        _CACHE[key] = (now, rows)
    return rows


def clear_cache() -> None:
    """Drop the memoized catalogs (test hook / after a skill install)."""
    with _CACHE_LOCK:
        _CACHE.clear()


# --- Public builder ----------------------------------------------------------

def build_catalog(profile: Optional[str] = None) -> dict[str, Any]:
    """Build the catalog payload for *profile* (see the module docstring for the contract)."""
    requested = (profile or "").strip() or None
    known = known_profiles()
    canon = resolve_profile(requested) if requested is not None else None
    if canon is not None:
        rows = _cached_rows(("profile", canon), lambda: _profile_rows(canon))
        scope, resolved = "profile", True
    else:
        rows = _cached_rows(("catalog", tuple(known)), lambda: _catalog_rows(known))
        scope, resolved = "catalog", requested is None
    return _payload(scope, canon, requested, resolved, known, rows)


def _profile_rows(profile: str) -> list[dict[str, Any]]:
    """Applicable skills for one profile, each tagged with the profile it applies to."""
    rows = [r for r in _scoped_skills(profile) if r["applicable"]]
    for row in rows:
        row["profiles"] = [profile]
    return rows


def _catalog_rows(known: list[str]) -> list[dict[str, Any]]:
    """Union of everything installed anywhere, flagged with where it applies."""
    merged: dict[str, dict[str, Any]] = {}
    for name in known:
        for row in _scoped_skills(name):
            entry = merged.get(row["id"])
            if entry is None:
                merged[row["id"]] = entry = {**row, "applicable": False}
            if row["applicable"]:
                entry["applicable"] = True
                entry["profiles"].append(name)
            # First-wins metadata, but never keep an empty description over a real one.
            if not entry["description"] and row["description"]:
                entry["description"] = row["description"]
            if entry["category"] is None:
                entry["category"] = row["category"]
    return list(merged.values())


def _payload(scope: str, canon: Optional[str], requested: Optional[str], resolved: bool,
             known: list[str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Deep enough copy that a caller mutating its response cannot poison the cache.
    skills = _sorted([{**r, "profiles": list(r["profiles"])} for r in rows])
    return {
        "scope": scope,
        "profile": canon,
        "requested_profile": requested,
        "resolved": resolved,
        "profiles": known,
        "count": len(skills),
        "skills": skills,
    }
