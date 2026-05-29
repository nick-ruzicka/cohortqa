"""End-to-end smoke test for PersonaRunner.

Drives the runner against a Playwright-intercepted route that serves a
small synthetic HTML page per route — no dev server, no network, no
external dependencies beyond chromium being installed.

Skipped automatically if Playwright + chromium aren't available so the
schema tests still pass in environments without browsers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the whole module if playwright isn't importable (e.g. CI without it).
playwright = pytest.importorskip("playwright.async_api")


# ─── Synthetic pages ──────────────────────────────────────────────────────────

SYNTHETIC_PAGES = {
    "/today": """
        <!doctype html><html><head><title>Today</title></head>
        <body>
          <h1>Today's roles</h1>
          <button>Run Scan</button>
          <table>
            <tr role="button" class="cursor-pointer"><td>Role A</td></tr>
          </table>
        </body></html>
    """,
    "/pipeline": """
        <!doctype html><html><head><title>Pipeline</title></head>
        <body>
          <h1>Pipeline</h1>
          <button>Run Scan</button>
          <button>Signal Scan</button>
          <table>
            <tr role="button" class="cursor-pointer">
              <td>Role X</td>
              <td><a href="/companies/acme">Acme</a></td>
              <td><a href="https://jobs.example.com/x" target="_blank">Apply</a></td>
            </tr>
          </table>
          <button>Skipped</button>
          <button>Evaluated</button>
        </body></html>
    """,
    # /companies, /signals, /context, /companies/[slug] return minimal
    # pages that load but offer no matching actions — the runner should
    # log "no affordance found" reasoning events.
    "_DEFAULT_": """
        <!doctype html><html><head><title>Page</title></head>
        <body><h1>Other page</h1></body></html>
    """,
}


def _make_app_config(tmp_path: Path) -> dict:
    """A minimal app config exercising the runner's main paths."""
    return {
        "app": {
            "name": "SmokeApp",
            "dev_server": "http://test.local",
            "description": "Synthetic app for runner smoke test.",
        },
        "routes": [
            {
                "path": "/today",
                "purpose": "Smoke today.",
                "actions": ["run_scan", "click_role_row"],
                "expected_load_time_ms": 5000,
            },
            {
                "path": "/pipeline",
                "purpose": "Smoke pipeline.",
                "actions": [
                    "run_scan",
                    "click_role_row",
                    "mark_skipped",
                    "open_company_from_row",
                ],
                "expected_load_time_ms": 5000,
            },
        ],
        "actions": [
            {"name": "run_scan", "selector": 'button:has-text("Run Scan")'},
            {
                "name": "click_role_row",
                "selector": 'tr[role="button"]',
                "requires": ["role_visible"],
            },
            {"name": "mark_skipped", "selector": 'button:has-text("Skipped")'},
            {
                "name": "open_company_from_row",
                "selector": 'a[href^="/companies/"]',
            },
        ],
        "friction_signals": [
            {"type": "navigation", "description": "Lost."},
            {"type": "slow_load", "description": "Slow."},
        ],
        "personas_dir": "personas",
        "scenarios_dir": "scenarios",
        "runs_dir": str(tmp_path / "runs"),
    }


def _persona() -> dict:
    return {
        "identity": {
            "name": "Smoke Persona",
            "role": "Test",
            "background": "Smokes tests for a living.",
        },
        "target_archetypes": ["smoke"],
        "location_preferences": [],
        "comp_floor": 100000,
        "behavioral": {
            "click_speed": "fast",          # keep test fast: 300ms delays
            "reads_details": True,
            "rejection_threshold": "low",   # so mark_skipped is chosen
            "detail_dwell_ms": 100,
        },
        "meta_attitude": "Smoke-testing.",
        "friction_sensitivities": ["navigation"],
    }


# ─── The smoke tests ──────────────────────────────────────────────────────────
# Driven via asyncio.run from sync test functions so pytest-asyncio isn't
# a required dependency.


def test_runner_smoke_end_to_end(tmp_path):
    """Run a session through two synthetic routes; verify the JSONL log
    has the events we'd expect to see."""
    import asyncio

    from cohortqa.core.runner import PersonaRunner, read_session

    async def install_intercept(context):
        async def handler(route, request):
            # Strip the dev_server prefix and look up by path.
            path = "/" + request.url.split("/", 3)[-1] if "://" in request.url else request.url
            # request.url is "http://test.local/today" → path becomes "/today"
            # Re-parse robustly:
            import urllib.parse
            parsed = urllib.parse.urlparse(request.url)
            page_path = parsed.path or "/"
            body = SYNTHETIC_PAGES.get(page_path, SYNTHETIC_PAGES["_DEFAULT_"])
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=body,
            )

        await context.route("**/*", handler)

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=_make_app_config(tmp_path),
        headless=True,
        context_hook=install_intercept,
    )

    summary = asyncio.run(runner.run())

    # ─── Assertions on the summary ────────────────────────────────────────────
    assert summary["persona_id"] == "smoke"
    assert summary["routes_visited"] == 2
    assert summary["actions_taken"] >= 2  # at least run_scan + one row action
    assert summary["session_path"], summary

    # ─── Assertions on the JSONL on disk ──────────────────────────────────────
    session_path = Path(summary["session_path"])
    assert session_path.exists()
    events = read_session(session_path)
    assert len(events) >= 5  # opening reasoning + 2 navs + actions

    # Every event tagged with cohortqa source for analytics filtering.
    for ev in events:
        assert ev["source"] == "cohortqa:smoke", ev

    # We hit both routes.
    nav_routes = [ev["route"] for ev in events if ev["event_type"] == "nav"]
    assert nav_routes == ["/today", "/pipeline"]

    # At least one action recorded with the run_scan name.
    actions = [ev for ev in events if ev["event_type"] == "action"]
    assert any(ev["action"] == "run_scan" for ev in actions), actions

    # Every nav event has render_time_ms and page_state.
    for ev in events:
        if ev["event_type"] == "nav":
            assert isinstance(ev["render_time_ms"], int)
            assert ev["render_time_ms"] >= 0
            assert ev["page_state"] is not None
            assert ev["page_state"]["url"].endswith(ev["route"])


def test_runner_logs_missing_affordances(tmp_path):
    """If a persona would take an action that the page doesn't expose,
    the runner records a reasoning event flagging the gap."""
    import asyncio

    from cohortqa.core.runner import PersonaRunner, read_session

    async def empty_intercept(context):
        async def handler(route, request):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<!doctype html><html><body><h1>Empty</h1></body></html>",
            )
        await context.route("**/*", handler)

    cfg = _make_app_config(tmp_path)
    # Trim to one route to keep the assertion focused.
    cfg["routes"] = cfg["routes"][:1]

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=cfg,
        headless=True,
        context_hook=empty_intercept,
    )
    asyncio.run(runner.run())

    events = read_session(runner.session_path)
    missing_reasons = [
        ev for ev in events
        if ev["event_type"] == "reasoning"
        and ev.get("reasoning") and "no matching affordance" in ev["reasoning"]
    ]
    assert missing_reasons, [
        (e["event_type"], e.get("reasoning")) for e in events
    ]


# ─── Protected-action suppression ─────────────────────────────────────────────

def test_runner_logs_intent_but_does_not_click_protected_actions(tmp_path):
    """``mark_skipped`` declares ``writes:applications.md`` — the runner
    must record persona intent but never dispatch the click. The
    distinction matters when CohortQA points at the real dashboard:
    a clicked button would mutate user data.

    We assert via the JSONL: a reasoning event with ``action == "mark_skipped"``
    and "intent logged, click suppressed" wording; and the absence of any
    ``event_type == "action"`` event for ``mark_skipped``.
    """
    import asyncio

    from cohortqa.core.runner import PersonaRunner, read_session

    async def intercept(context):
        async def handler(route, request):
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=SYNTHETIC_PAGES["/pipeline"],
            )
        await context.route("**/*", handler)

    cfg = _make_app_config(tmp_path)
    # Limit to /pipeline so the assertions stay focused on the one route.
    cfg["routes"] = [cfg["routes"][1]]
    # Mark mark_skipped as protected (matches the qa/app.yaml convention).
    for action in cfg["actions"]:
        if action["name"] == "mark_skipped":
            action["side_effects"] = [
                "emits_event:role.status_changed",
                "writes:applications.md",
            ]

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=cfg,
        headless=True,
        context_hook=intercept,
    )
    asyncio.run(runner.run())
    events = read_session(runner.session_path)

    actions_clicked = [
        ev for ev in events
        if ev["event_type"] == "action" and ev.get("action") == "mark_skipped"
    ]
    intent_logs = [
        ev for ev in events
        if ev["event_type"] == "reasoning"
        and ev.get("action") == "mark_skipped"
        and ev.get("reasoning")
        and "intent logged" in ev["reasoning"]
    ]

    assert not actions_clicked, (
        "mark_skipped was clicked despite writes:applications.md side-effect: "
        f"{actions_clicked!r}"
    )
    assert intent_logs, (
        "expected a reasoning event recording mark_skipped intent "
        f"with 'intent logged' wording; got events: "
        f"{[(e['event_type'], e.get('action')) for e in events]}"
    )


# ─── Detail-route traversal ───────────────────────────────────────────────────

def test_runner_walks_into_detail_route_after_navigation(tmp_path):
    """When a parent-route action navigates to a path matching a declared
    ``detail_routes`` entry, the runner exercises the detail route's
    actions in place. This is what makes C6 (validate /companies/[slug])
    work — without detail traversal, the company drilldown is never
    actually visited.
    """
    import asyncio

    from cohortqa.core.runner import PersonaRunner, read_session

    # Two pages: a /companies list with a link to /companies/acme, and the
    # detail page itself with a Back button.
    pages = {
        "/companies": """
            <!doctype html><html><head><title>Companies</title></head>
            <body>
              <h1>Companies</h1>
              <a href="/companies/acme">Acme</a>
            </body></html>
        """,
        "/companies/acme": """
            <!doctype html><html><head><title>Acme</title></head>
            <body>
              <h1>Acme — Company drilldown</h1>
              <a href="/companies">Back</a>
            </body></html>
        """,
    }

    async def intercept(context):
        async def handler(route, request):
            from urllib.parse import urlparse
            path = urlparse(request.url).path or "/"
            body = pages.get(path, "<!doctype html><html><body>?</body></html>")
            await route.fulfill(status=200, content_type="text/html", body=body)
        await context.route("**/*", handler)

    cfg = {
        "app": {
            "name": "DetailApp",
            "dev_server": "http://test.local",
            "description": "Detail route smoke.",
        },
        "routes": [
            {
                "path": "/companies",
                "purpose": "Companies list.",
                "actions": ["open_company_detail"],
                "expected_load_time_ms": 5000,
                "detail_routes": [
                    {
                        "path": "/companies/[slug]",
                        "purpose": "Company drilldown.",
                        "actions": ["back_to_companies"],
                        "expected_load_time_ms": 5000,
                    },
                ],
            },
        ],
        "actions": [
            {
                "name": "open_company_detail",
                "selector": 'a[href^="/companies/"]:not([href="/companies"])',
                "side_effects": ["navigates_to:/companies/[slug]"],
            },
            {
                "name": "back_to_companies",
                "selector": 'a[href="/companies"]',
            },
        ],
        "friction_signals": [{"type": "navigation", "description": "Lost."}],
        "personas_dir": "personas",
        "scenarios_dir": "scenarios",
        "runs_dir": str(tmp_path / "runs"),
    }

    runner = PersonaRunner(
        persona=_persona(),
        persona_id="smoke",
        app_config=cfg,
        headless=True,
        context_hook=intercept,
    )
    asyncio.run(runner.run())

    events = read_session(runner.session_path)
    routes_navigated = [ev["route"] for ev in events if ev["event_type"] == "nav"]

    assert "/companies" in routes_navigated, routes_navigated
    assert "/companies/[slug]" in routes_navigated, (
        f"detail route not visited; routes seen: {routes_navigated}"
    )

    # The detail-route nav event records how we got there.
    detail_nav = next(
        ev for ev in events
        if ev["event_type"] == "nav" and ev["route"] == "/companies/[slug]"
    )
    assert detail_nav["page_state"]["entered_via"] == "detail_route_traversal", detail_nav
    assert detail_nav["page_state"]["url"].endswith("/companies/acme"), detail_nav


def test_matches_path_pattern_dynamic_segment():
    """Direct test of the pattern matcher — easier to reason about than
    only exercising it through the runner."""
    from cohortqa.core.runner import _matches_path_pattern

    assert _matches_path_pattern("/companies/anthropic", "/companies/[slug]")
    assert _matches_path_pattern("/users/42/posts", "/users/[id]/posts")
    # Wrong arity
    assert not _matches_path_pattern("/companies", "/companies/[slug]")
    assert not _matches_path_pattern(
        "/companies/anthropic/roles", "/companies/[slug]"
    )
    # Static segment must match exactly
    assert not _matches_path_pattern("/orgs/anthropic", "/companies/[slug]")
    # Empty dynamic segment is rejected
    assert not _matches_path_pattern("/companies/", "/companies/[slug]")
