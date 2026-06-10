# Brief Loading UI — Design Spec
Date: 2026-06-10

## Problem

Clicking "Generate brief" on the dashboard navigates to `/brief`, which blocks the entire
page load while running AI generation (5–15s). There is no visual feedback during this wait.
The "Generate Now" button on the brief page has only a minimal `"Generating…"` text indicator
that is easy to miss.

## Approach: Instant page load + auto-triggered generation

Render `/brief` immediately with a loading skeleton, then auto-fire the generation as an HTMX
request on page load. The user sees the page instantly; content streams in when ready.

---

## Components

### 1. `/brief` route (`postmind/web/server.py`)

**Change:** Replace `DailyBriefGenerator(account_email).get_or_generate(force=False)` with a
fast DB-only lookup via `DailyBriefRepo.get_today(account_email)`.

- If a brief exists today → render normally (no behavior change).
- If no brief today → set `brief=None`, `auto_generate=True` in template context. No AI call
  on the GET request. Page renders in milliseconds.

### 2. `daily_brief.html` — loading skeleton

When `auto_generate=True`:

- **Stat cards section:** hide (or show zeroes/dashes) — will be populated via OOB swap after generation.
- **`#brief-content` div:** render an animated loading card instead of the empty-state message:
  - Pulsing/spinning icon
  - Text: "Analyzing your inbox…"
  - Sub-text: "This usually takes 5–15 seconds"
  - Add HTMX attributes to auto-fire generation on page load:
    ```html
    hx-post="/brief/generate"
    hx-trigger="load"
    hx-swap="outerHTML"
    ```

When brief already exists, page renders exactly as today — no change.

### 3. `/brief/generate` response — OOB stat card update

After generation completes, the HTMX response currently returns HTML for `#brief-content`
only. Extend it to include an out-of-band snippet (`hx-swap-oob="true"`) that replaces the
stat cards section with the real values — so numbers appear without requiring a full page
reload.

The stat cards section gets a stable `id="brief-stat-cards"` so the OOB swap can target it.

### 4. "Generate Now" button polish

Free improvement while touching the template:

- Add a CSS spin animation to the refresh icon while the HTMX request is in flight (using the
  `.htmx-request` class selector on the button).
- Disable the button during the request (`:disabled` state via `htmx-request` class or
  `hx-disabled-elt="this"`).

### 5. Dashboard — no changes

The `<a href="/brief">Generate brief</a>` link stays as-is. Since `/brief` now loads
instantly, no dashboard changes are needed.

---

## Data flow

```
User clicks "Generate brief" (dashboard)
  → GET /brief
  → DB lookup (fast, ~10ms)
  → Render page immediately with skeleton in #brief-content
  → Browser fires hx-post="/brief/generate" on load
  → Server: run_in_executor → DailyBriefGenerator.get_or_generate(force=True)
  → Returns: #brief-content HTML + OOB #brief-stat-cards HTML
  → HTMX swaps both into the page
```

---

## Error handling

The existing error path in `/brief/generate` already returns an error card for `#brief-content`.
No new error handling needed — the skeleton just gets replaced with the error message.

---

## Testing

- Navigate to `/brief` with no brief for today → page loads instantly, skeleton visible, content
  populates after generation.
- Navigate to `/brief` with a brief already generated today → page renders immediately with full
  content (no skeleton, no auto-trigger).
- Click "Generate Now" on an existing brief → refresh icon spins, button disabled during request.
- Confirm stat cards update (unread, high priority, follow-ups) after auto-generation without
  page reload.
