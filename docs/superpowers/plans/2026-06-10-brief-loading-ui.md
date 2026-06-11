# Brief Loading UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/brief` load instantly with a loading skeleton and auto-trigger AI generation via HTMX, instead of blocking the page load for 5–15 seconds.

**Architecture:** The `/brief` GET route switches from a blocking `get_or_generate()` call to a fast DB-only lookup. When no brief exists today, the page renders a skeleton in `#brief-content` that auto-fires `POST /brief/generate` on load via HTMX. The generate response includes an out-of-band swap that fills in the stat cards. Stat cards move to a shared partial so the page and the OOB response render the same markup.

**Tech Stack:** FastAPI, Jinja2, HTMX (already loaded in `base.html`), pytest + TestClient.

**Spec:** `docs/superpowers/specs/2026-06-10-brief-loading-ui-design.md`

---

## File map

- Create: `postmind/web/templates/_brief_stat_cards.html` — stat-cards partial (page + OOB)
- Modify: `postmind/web/server.py` — `/brief` route (~line 1081), `/brief/generate` response (~line 1210)
- Modify: `postmind/web/templates/daily_brief.html` — skeleton, auto-trigger, button polish
- Create: `tests/test_brief_page.py` — route tests

---

### Task 1: `/brief` route — fast DB lookup + skeleton + auto-trigger

**Files:**
- Create: `tests/test_brief_page.py`
- Modify: `postmind/web/server.py:1081-1127` (`brief_page`)
- Modify: `postmind/web/templates/daily_brief.html`
- Create: `postmind/web/templates/_brief_stat_cards.html`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_brief_page.py` (fixture pattern copied from `tests/test_learning_web.py`):

```python
"""Tests for the /brief page fast-load + auto-generate skeleton behaviour."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.core.storage as st
import postmind.web.server as s
from postmind.core.storage import DailyBrief

ACCOUNT = "me@example.com"


@pytest.fixture(autouse=True)
def _shared_db(monkeypatch):
    """StaticPool so the executor thread sees the same in-memory DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    st.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(st, "_engine", engine)
    monkeypatch.setattr(st, "_SessionLocal", factory)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(s, "_get_web_account", lambda: ACCOUNT)
    return TestClient(s.app, raise_server_exceptions=True)


def _seed_brief() -> None:
    session = st.get_session()
    session.add(DailyBrief(
        account_email=ACCOUNT,
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="**Test brief** content here.",
        ai_used=True,
        unread_count=5,
        new_since_yesterday=2,
        high_priority_count=1,
        overdue_followups_count=0,
        generated_at=datetime.now(timezone.utc),
    ))
    session.commit()
    session.close()


def test_brief_page_no_brief_renders_skeleton_without_generating(client, monkeypatch):
    """With no brief today, /brief must NOT generate inline — it renders a
    skeleton that auto-fires POST /brief/generate via HTMX."""
    import postmind.core.daily_brief as db_mod

    def _boom(*a, **kw):
        raise AssertionError("DailyBriefGenerator must not be constructed on GET /brief")

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _boom)

    resp = client.get("/brief")
    assert resp.status_code == 200
    html = resp.text
    # auto-trigger wiring on the swap target
    assert 'hx-post="/brief/generate"' in html
    assert 'hx-trigger="load"' in html
    # skeleton copy
    assert "Analyzing your inbox" in html
    # stat-cards OOB target exists even before generation
    assert 'id="brief-stat-cards"' in html


def test_brief_page_existing_brief_renders_content_no_autotrigger(client, monkeypatch):
    _seed_brief()
    import postmind.core.daily_brief as db_mod

    def _boom(*a, **kw):
        raise AssertionError("DailyBriefGenerator must not be constructed on GET /brief")

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _boom)

    resp = client.get("/brief")
    assert resp.status_code == 200
    html = resp.text
    assert "Test brief" in html
    # No auto-fire when brief exists. NOTE: must be this exact string — the page
    # also contains an unrelated `hx-get="/rules/proposals" hx-trigger="load"`.
    assert 'hx-post="/brief/generate" hx-trigger="load"' not in html
    assert 'id="brief-stat-cards"' in html
    assert ">5</p>" in html  # unread count rendered in stat card
```

Note: if `DailyBrief` requires other non-nullable columns, check the model in
`postmind/core/storage.py` and add them to `_seed_brief`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_brief_page.py -v`
Expected: both FAIL — the first because `brief_page` constructs `DailyBriefGenerator` (raises `AssertionError("...must not be constructed...")`), the second on the missing `id="brief-stat-cards"` marker.

- [ ] **Step 3: Create the stat-cards partial**

Create `postmind/web/templates/_brief_stat_cards.html`. Content is the stat-cards grid
currently inlined in `daily_brief.html` lines 50–77, wrapped in an identifiable div:

```html
{# Stat mini-cards — shared by /brief page render and the /brief/generate OOB swap.
   Context: brief (may be None), auto_generate (bool), oob (bool, set by /brief/generate). #}
<div id="brief-stat-cards" {% if oob %}hx-swap-oob="true"{% endif %}>
  {% if brief %}
  <div class="grid grid-cols-3 gap-4 mb-6">
    <div class="pm-card p-4">
      <p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em]">Unread</p>
      <p class="text-2xl font-semibold text-ink mt-1 tabular">{{ brief.unread_count }}</p>
      <p class="text-ink-tertiary text-xs mt-1">{{ brief.new_since_yesterday }} new since yesterday</p>
    </div>
    <div class="pm-card p-4">
      <p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em]">High priority</p>
      <p class="text-2xl font-semibold {% if brief.high_priority_count > 0 %}text-warning{% else %}text-ink{% endif %} mt-1 tabular">{{ brief.high_priority_count }}</p>
      <p class="text-ink-tertiary text-xs mt-1">need attention</p>
    </div>
    <div class="pm-card p-4">
      <p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em]">Follow-ups overdue</p>
      <p class="text-2xl font-semibold {% if brief.overdue_followups_count > 0 %}text-danger{% else %}text-ink{% endif %} mt-1 tabular">{{ brief.overdue_followups_count }}</p>
      <p class="text-ink-tertiary text-xs mt-1">no reply yet</p>
    </div>
  </div>
  {% elif auto_generate %}
  <div class="grid grid-cols-3 gap-4 mb-6">
    {% for label in ["Unread", "High priority", "Follow-ups overdue"] %}
    <div class="pm-card p-4">
      <p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em]">{{ label }}</p>
      <div class="h-8 w-12 mt-1 rounded bg-surface-2 animate-pulse"></div>
      <div class="h-3 w-24 mt-2 rounded bg-surface-2 animate-pulse"></div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="pm-card p-8 text-center mb-6">
    <p class="text-ink-tertiary text-sm">No brief generated yet today — hit "Generate Now" above.</p>
    <p class="text-ink-tertiary/60 text-xs mt-1.5">
      Or enable <strong>Generate daily brief</strong> in
      <a href="/agents" class="text-accent underline">Agents → Automation</a>
      to auto-generate on the first heartbeat each morning.
    </p>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 4: Update `daily_brief.html` to use the partial + skeleton**

In `postmind/web/templates/daily_brief.html`:

4a. Replace the entire stat-cards block (lines 49–77, from `<!-- Stat mini-cards -->`
through the `{% endif %}` after the "No brief generated yet today" card) with:

```html
  <!-- Stat mini-cards (also the OOB swap target for /brief/generate) -->
  {% include "_brief_stat_cards.html" %}
```

4b. Change the `#brief-content` div opening tag (line 94) to carry the auto-trigger:

```html
    <div class="px-5 py-5" id="brief-content"
         {% if auto_generate %}hx-post="/brief/generate" hx-trigger="load" hx-swap="outerHTML"{% endif %}>
```

4c. Replace the `{% else %}` empty-state inside `#brief-content` (the
"No brief for today yet." block, lines 102–109) with:

```html
      {% else %}
      {% if auto_generate %}
      <div class="text-center py-10">
        <svg class="w-8 h-8 mx-auto text-accent animate-spin mb-3" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/>
        </svg>
        <p class="text-ink text-sm font-medium">Analyzing your inbox&hellip;</p>
        <p class="text-ink-tertiary text-xs mt-1">This usually takes 5&ndash;15 seconds</p>
      </div>
      {% else %}
      <div class="text-center py-10">
        <svg class="w-8 h-8 mx-auto text-ink-tertiary/50 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
        </svg>
        <p class="text-ink-tertiary text-sm">No brief for today yet.</p>
        <p class="text-ink-tertiary/70 text-xs mt-1">Hit "Generate Now" above to build one.</p>
      </div>
      {% endif %}
      {% endif %}
```

- [ ] **Step 5: Update the `/brief` route to DB-only lookup**

In `postmind/web/server.py`, `brief_page` (line 1081). Replace the executor call
(lines 1097–1100):

```python
    today_str = _dt.now(_tz.utc).date().isoformat()
    loop = asyncio.get_event_loop()
    brief = await loop.run_in_executor(
        _executor, lambda: DailyBriefGenerator(account_email).get_or_generate(force=False)
    )
    session = get_session()
    recent = DailyBriefRepo(session).list_recent(account_email, limit=7)
    session.close()
```

with a fast DB lookup (drop the now-unused `DailyBriefGenerator` import and the
`loop = asyncio.get_event_loop()` line):

```python
    today_str = _dt.now(_tz.utc).date().isoformat()
    session = get_session()
    repo = DailyBriefRepo(session)
    brief = repo.get_today(account_email, today_str)
    recent = repo.list_recent(account_email, limit=7)
    session.close()
```

Then add the flag to the context dict (in the existing `ctx.update({...})`):

```python
            "auto_generate": brief is None,
            "oob": False,
```

(`oob: False` keeps the partial's `{% if oob %}` well-defined on page render.)

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_brief_page.py -v`
Expected: both PASS. If `_seed_brief` fails on a missing non-nullable column, read the
`DailyBrief` model in `postmind/core/storage.py` and add the column to the seed.

- [ ] **Step 7: Run full suite + lint, then commit**

Run: `make fix && .venv/bin/python -m pytest tests/ -q --tb=short`
Expected: all pass, no lint errors.

```bash
git add tests/test_brief_page.py postmind/web/server.py postmind/web/templates/daily_brief.html postmind/web/templates/_brief_stat_cards.html
git commit -m "feat(brief): instant page load with auto-generate skeleton"
```

---

### Task 2: `/brief/generate` — OOB stat-card swap

**Files:**
- Modify: `postmind/web/server.py:1160-1217` (`brief_generate`)
- Test: `tests/test_brief_page.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brief_page.py`:

```python
def test_brief_generate_includes_oob_stat_cards(client, monkeypatch):
    """POST /brief/generate response must carry an hx-swap-oob block that
    updates #brief-stat-cards with the freshly generated counts."""
    from types import SimpleNamespace

    import postmind.core.daily_brief as db_mod

    fake_brief = SimpleNamespace(
        content="Fresh **brief**.",
        ai_used=False,
        generated_at=datetime.now(timezone.utc),
        unread_count=7,
        new_since_yesterday=3,
        high_priority_count=2,
        overdue_followups_count=1,
        avoided_count=0,
        items_json=None,
        deals_json=None,
        newsletters_json=None,
        promotions_json=None,
        digest_trash_after=None,
    )

    class _FakeGen:
        def __init__(self, account_email):
            pass

        def get_or_generate(self, force=False):
            return fake_brief

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _FakeGen)

    resp = client.post("/brief/generate")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="brief-content"' in html
    assert "Fresh" in html
    # OOB stat-cards block with the new counts
    assert 'id="brief-stat-cards"' in html
    assert "hx-swap-oob" in html
    assert ">7</p>" in html  # unread
    assert ">2</p>" in html  # high priority
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_brief_page.py::test_brief_generate_includes_oob_stat_cards -v`
Expected: FAIL — `hx-swap-oob` not in response.

- [ ] **Step 3: Add the OOB block to the response**

In `postmind/web/server.py`, `brief_generate`, just before the final `return HTMLResponse(...)`
(line ~1210), render the partial:

```python
    stat_cards_oob = templates.env.get_template("_brief_stat_cards.html").render(
        brief=brief, oob=True, auto_generate=False
    )
```

and append it to the response body:

```python
    return HTMLResponse(
        f'<div id="brief-content" class="px-5 py-5">'
        f'<div class="flex items-center gap-2 mb-4">{ai_badge}'
        f'<span class="text-ink-tertiary text-xs">Generated at {gen_time}</span></div>'
        f"{status_html}{links_html}{content_html}"
        f"</div>"
        f"{stat_cards_oob}"
        f"{digest_init}"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_brief_page.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_brief_page.py postmind/web/server.py
git commit -m "feat(brief): OOB stat-card swap after generation"
```

---

### Task 3: "Generate Now" button polish

**Files:**
- Modify: `postmind/web/templates/daily_brief.html:13-25`

No unit test — visual behavior, verified in Task 4 via browser.

- [ ] **Step 1: Update the button markup**

Replace the Generate Now button (lines 13–25 of `daily_brief.html`) with:

```html
    <button
      id="brief-generate-btn"
      hx-post="/brief/generate"
      hx-target="#brief-content"
      hx-swap="outerHTML"
      hx-disabled-elt="this"
      class="pm-btn-secondary flex items-center gap-2 disabled:opacity-60 disabled:cursor-not-allowed">
      <!-- Lucide: refresh-cw -->
      <svg id="brief-gen-icon" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">
        <path stroke-linecap="round" stroke-linejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
      </svg>
      <span class="brief-gen-label">Generate Now</span>
      <span class="brief-gen-busy hidden text-xs">Generating&hellip;</span>
    </button>
```

- [ ] **Step 2: Add the in-flight CSS**

Add a small style block at the top of the `{% block content %}` in `daily_brief.html`
(right after line 3):

```html
<style>
  #brief-generate-btn.htmx-request #brief-gen-icon { animation: spin 1s linear infinite; }
  #brief-generate-btn.htmx-request .brief-gen-label { display: none; }
  #brief-generate-btn.htmx-request .brief-gen-busy { display: inline; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
```

(HTMX adds `htmx-request` to the triggering element while the request is in flight;
`hx-disabled-elt="this"` disables the button for the duration.)

- [ ] **Step 3: Run full suite + commit**

Run: `make fix && .venv/bin/python -m pytest tests/ -q --tb=short`
Expected: all pass.

```bash
git add postmind/web/templates/daily_brief.html
git commit -m "feat(brief): spinner + disabled state on Generate Now button"
```

---

### Task 4: Browser verification (per CLAUDE.md — mandatory, autonomous)

**Files:** none (verification only)

- [ ] **Step 1: Restart the server**

```bash
pkill -f "postmind serve" 2>/dev/null; sleep 1; .venv/bin/postmind serve > /tmp/postmind.log 2>&1 &
sleep 3
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8484/
```
Expected: `200`.

- [ ] **Step 2: Verify fresh-generation flow with Playwright MCP**

Using `mcp__plugin_playwright_playwright__*` tools:

1. Navigate to `http://127.0.0.1:8484/brief`.
2. If a brief already exists for today (real DB), first click "Generate Now" and confirm:
   icon spins, button is disabled, label swaps to "Generating…", and the brief content +
   stat cards update on completion without a page reload.
3. To test the auto-generate path on a fresh day-state: delete today's brief row
   (`sqlite3 ~/.postmind/postmind.db "DELETE FROM daily_briefs WHERE brief_date = date('now');"`),
   reload `/brief`, and confirm: page renders instantly with pulsing stat-card skeletons
   and the spinning "Analyzing your inbox…" card, then content + stat cards fill in
   when generation finishes.
4. Screenshot the skeleton state and the completed state.
5. Check the browser console for JS errors (`browser_console_messages`).

- [ ] **Step 3: Verify the existing-brief path**

Reload `/brief` (brief now exists): page renders fully populated with no skeleton and
no auto-trigger network call to `/brief/generate` (check via `browser_network_requests`).

- [ ] **Step 4: Final check + report**

Run: `make check`
Expected: lint + format + security + tests all pass.

Report observed browser behavior to the user with screenshots.
