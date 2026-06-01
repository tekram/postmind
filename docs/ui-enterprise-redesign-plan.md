# postmind — Enterprise UI Lift Plan

**Date:** 2026-05-31
**Goal:** Make postmind look "super enterprise" — premium, polished, precise — **without changing any functionality.** This is a *reskin*, not a re-architecture. Every route, form, button, and behavior that exists today must still exist and work identically afterward.

---

## 1. The Bet: Linear

We adopt the **Linear** design language (from `voltagent/awesome-design-md` → `design-md/linear/DESIGN.md`).

Two independent assessments (the author's and a separate research agent's) converged on Linear as the best fit. Rationale:

- Linear is the canonical **"quietly enterprise / software-craft"** aesthetic — hierarchy from precise spacing, a tight neutral ramp, and 1px hairlines rather than gimmicks, gradients, or heavy shadows.
- It maps almost 1:1 onto postmind's existing shape: a **dark sidebar + content area, single accent color, card-based layouts, a data-dense sortable table.** We are mostly *retoning and tightening*, not restructuring.
- It ships cleanly in **Tailwind + htmx with no build step** — pure hex tokens, free Inter font substitute, fixed radii, flat depth.

**Runner-up (not chosen):** Vercel — equally clean and build-free, but its signature is a light marketing-site aesthetic with a hero mesh gradient, which is less native to a dark-sidebar *app* shell.

### Executive deviations from stock Linear (deliberate)

| Decision | Choice | Why |
|---|---|---|
| **Accent color** | **Keep postmind teal** (`#0d9488` family) instead of Linear's lavender `#5e6ad2` | Teal is brand-load-bearing across the product. Linear's *discipline* (one scarce accent, used sparingly) is what reads "enterprise" — the hue itself is secondary. Single-accent teal satisfies the discipline. |
| **Light vs dark content** | **Keep dark sidebar + light content** | Lowest-risk reskin; matches mainstream enterprise SaaS dashboards (Linear's own *app* is dark, but its light surfaces follow the same neutral rigor). Full dark mode is a deferred optional phase, not in scope. |
| **Semantics** | Add explicit danger/warning/success tokens | Linear's spec only documents success-green. postmind's purge/undo/protected flows need clear red/amber/green. We define them once. |
| **Icons** | **Replace all emoji with Lucide line icons** | Emoji (📊 🤖 🔄 ↩ ⚙️ ✂ ⚠) clash hard with the craft aesthetic. One consistent stroke-1.75 line set lands the "enterprise" read. |
| **Font** | **Inter** (500/600/700), loaded once | Documented free substitute for Linear's proprietary type. Tabular figures for the numbers-heavy Clean Up table. |

---

## 2. Design Tokens (the system)

Defined **once** in `base.html` as Tailwind config + CSS variables, so every page inherits them. No per-page color literals after this.

**Typography**
- Family: `Inter`, fallback `ui-sans-serif, system-ui, -apple-system, sans-serif`. Mono: `JetBrains Mono`/`ui-monospace`.
- Display/H1 28px / 600 / tracking -0.02em. H2 22px / 600 / -0.01em. Card title 16px / 600. Body 14px / 400 / 1.5. Caption 12px / 500. Eyebrow 11–12px / 600 / **+0.06em** uppercase.
- Numerals in tables/stat cards: `font-variant-numeric: tabular-nums`.

**Color ramp (light content)**
- Canvas `#fafafa` (replaces slate-50). Surface-1 (cards) `#ffffff`. Surface-2 (hover/raised) `#f5f5f5`.
- Hairlines: border `#e7e7e9`, strong `#d4d4d8`. **No heavy shadows** — depth = surface + hairline; allow at most a `0 1px 2px rgba(0,0,0,0.04)` whisper on cards.
- Text: ink `#18181b`, muted `#52525b`, subtle `#71717a`, tertiary `#a1a1aa`.
- Sidebar (dark): canvas `#0a0a0b` (slightly warmer/flatter than slate-950), hairline `#1f1f23`, item text `#a1a1aa`, active item bg `#1a1a1d` + ink `#fafafa`.

**Accent (teal, kept)**
- accent `#0d9488`, hover `#0f766e`, subtle-bg `#f0fdfa`, subtle-border `#99f6e4`, on-accent `#ffffff`. Focus ring: `2px` accent @ ~40% opacity.

**Semantics**
- success `#16a34a` / bg `#f0fdf4`; warning `#d97706` / bg `#fffbeb`; danger `#dc2626` / bg `#fef2f2`. Each with a matching subtle border.

**Radii** — chip/badge 6px, button & input **8px**, card **12px**, large panel 16px, pill `9999` (status pills / toggles / avatars only). Never pill-round CTAs.

**Spacing** — 4px base: 4/8/12/16/24/32/48; page padding 32px; card padding 20–24px; button padding 8×14px; input 8×12px.

**Buttons**
- Primary: teal fill, white text, 8px radius, 8×14, ≥36px tall, `font-medium`.
- Secondary: white fill + 1px hairline, ink text, hover surface-2.
- Tertiary: transparent, accent or ink text.
- Danger: danger fill or danger hairline+text (for purge/remove).

**Motion** — restrained. 150ms ease color/bg transitions on interactive elements; subtle row hover; no atmospheric/gradient effects.

---

## 3. Current State Inventory (DOCUMENTATION)

Complete map of what exists today. **Stack:** FastAPI + Jinja2 + Tailwind (CDN) + htmx. **Shell:** `base.html` (fixed dark sidebar `w-52` + `ml-52` main) + floating chat assistant widget. **Templates:** 21 files. **Routes:** ~55.

### 3.1 Global shell — `base.html`
- Fixed dark left sidebar (`w-52`): brand mark + version, **AI-mode badge** (cloud/local/off), **account display / multi-account switcher** (`POST /accounts/switch`).
- Nav groups: top (**Super Agent** `/agent`, **Dashboard** `/`, **Clean Up** `/stats`), **Automate** (Triage `/triage`, Agents `/agents`, Watch `/watch`), **Manage** (Sync `/sync`, Accounts `/accounts`, Undo History `/undo`, Settings `/settings`). Active-state highlighting per page.
- Sidebar footer: "Runs 100% locally" note.
- **Floating assistant widget** (bottom-right): launcher button, slide-up panel, header with mode subtext + clear + close, message list (localStorage history, markdown-ish formatting, action buttons), composer textarea + send, `POST /chat` with streaming-ish fetch, auto-resize input, Enter-to-send.

### 3.2 Pages & functionality (each is a checklist row in §4)

| # | Page / Template | Route(s) | Functionality that MUST survive |
|---|---|---|---|
| P1 | **Dashboard** `dashboard.html` | `GET /` | Header (account email / "Inbox Overview" + last-scanned). Not-authenticated banner with `postmind auth` hint. 4 stat cards (emails cached, unique senders, reclaimable MB, last synced + re-sync link). "Recommended next step" card → Clean Up. Top-senders preview list (name/email/count/size/tier icon) + "View all". No-scan empty-state CTA ("Scan inbox"). 5 quick-action tiles (Clean Up, Triage, Sync, Undo, Settings). |
| P2 | **Clean Up** `stats.html` + `stats_table.html` | `GET /stats`, `GET /stats/data` (htmx) | Title "Clean Up Your Inbox". Sort controls, **scope** filter, **age/since** filter. htmx-loaded sortable sender table (`/stats/data`). Per-sender select + **bulk purge / archive** via `POST /purge/preview`. Tier/safety icons. |
| P3 | **Purge preview** `purge_preview.html` | `POST` & `GET /purge/preview`, `POST /purge/confirm` | Preview of impending purge/archive (count, size, sender list). "Confirm purge" / "Confirm archive" (label-aware). Cancel path. |
| P4 | **Triage** `triage.html` | `GET /triage`, `GET /triage/classify-stream` (SSE) | "Inbox Triage". "AI required" gating state. Streaming classification of emails into categories; per-item results render. |
| P5 | **Agents** `agents.html` | `GET /agents`, `/agents/daemon-badge`, `POST /agents/create|toggle|delete|soul|features|compose` | "Heartbeat Agents". Create-agent form. Per-agent toggle / delete. **Soul** (persona) editor, **features** toggles, **categories**. Daemon status badge (htmx poll). **Compose** assistant form (recipient, goal, thread paste) → `POST /agents/compose`. |
| P6 | **Watch** `watch.html` | `GET /watch`, `POST /watch/start|stop`, `GET /watch/status` | "Watch". Start/stop watcher. Live status (htmx poll). |
| P7 | **Sync** `sync.html` | `GET /sync`, `POST /sync/start`, `GET /sync/poll/{task_id}` | "Sync Inbox". Sync options form. Start sync (htmx) + progress polling to completion. |
| P8 | **Accounts** `accounts.html` | `GET /accounts`, `POST /accounts/switch|remove` | "Accounts". List accounts. Switch active. Remove account. Link to add. |
| P9 | **Add account** `accounts_add.html` | `GET /accounts/add`, `POST /accounts/add/gmail/start`, `GET /accounts/add/gmail/poll/{id}`, `POST /accounts/add/imap` | "Add account". Connect Gmail (OAuth start + poll). Connect IMAP (host/port/user/folder form). |
| P10 | **Onboarding** `onboarding.html` | `GET /onboarding`, `POST /onboarding/connect/imap`, `POST /onboarding/ai-mode`, `POST /onboarding/upload-credentials`, `POST /accounts/add/gmail/start` | Wizard: Welcome → Step 1 connect inbox (Gmail/IMAP) → Step 2 enable AI (mode + API key/local config + credential upload) → "You're all set!". |
| P11 | **Settings** `settings.html` | `GET /settings`, `POST /settings/ai-mode|chat|agent` | "Settings": AI Mode, Chat Assistant config, Super Agent config, Email Provider, Data Storage, Protected Senders section. Model/endpoint inputs (Claude model, Ollama URL, local model). |
| P12 | **Protected senders** `blocked.html` | `GET /settings/blocked`, `POST /settings/blocked/add|remove` | "Protected Senders". Add/remove protected sender by email. |
| P13 | **Undo history** `undo.html` | `GET /undo`, `POST /undo/{entry_id}` | "Undo History". List of reversible actions. Per-entry undo/restore. |
| P14 | **Super Agent** `agent.html` | `GET /agent`, `POST /agent`, `POST /agent/stream`, `/agent/create-agent`, `/agent/create-rule`, `/agent/action/preview`, `/agent/action/confirm`, `/agent/unsubscribe/confirm`, `/agent/send` | NL command center: header + AI-mode badge, example prompts, conversation stream, composer ("Run"). Inline result **cards** (create_agent, create_rule, action preview/confirm, unsubscribe confirm, send email). |
| P15 | **Agent action preview** `agent_action_preview.html` | `GET`/`POST /agent/action/preview`, `POST /agent/action/confirm` | Confirm panel for agent-proposed actions (purge/label/archive), label-aware verb. |
| P16 | **Chat backend** (`/chat`) | `POST /chat` | Floating-assistant backend (see shell). Returns reply + action links. |
| P17 | **Error / Blocked / Stats-error states** `error.html`, `blocked.html`, `stats_error.html` | rendered on failure | Error page, stats error state. Must keep rendering. |

---

## 4. UI Upgrade Checklist (the work)

> Each page is upgraded to the §2 token system. **Functionality is frozen** — only markup/classes/icons change. Do NOT touch route handlers, form `action`/`method`, `name=` fields, `hx-*` attributes, element `id`s used by JS, or JS logic.

### Phase 0 — Foundation (do first; everything depends on it)
- [ ] In `base.html`: load Inter (+ optional JetBrains Mono) and configure a **Tailwind `tailwind.config` theme** (still CDN) defining the §2 tokens as named colors (`canvas`, `surface`, `ink`, `accent`, `success`, `warning`, `danger`, hairline, etc.) + `borderRadius` + `tabular-nums` utility.
- [ ] Add a tiny set of reusable component classes (via `@layer`/inline `<style>` or documented class recipes): `.pm-btn`, `.pm-btn-secondary`, `.pm-btn-danger`, `.pm-card`, `.pm-input`, `.pm-badge`, `.pm-pill`, `.pm-table`. Pages consume these.
- [ ] Define a **Lucide icon convention** (inline SVG snippets) to replace every emoji and unify the existing hand-rolled SVGs at stroke-width 1.75.

### Phase 1 — Shell
- [ ] `base.html` sidebar: retone to warm-flat dark (`#0a0a0b`), hairline dividers, refined nav item spacing/active state, Inter, eyebrow group labels (+tracking). Replace `✂`/`✦` brand glyph with a clean mark. Keep AI badge + account switcher behavior.
- [ ] `base.html` floating assistant: re-skin launcher + panel + bubbles + composer to tokens (teal accent, 12px radii, hairlines). **Keep all element ids and JS untouched.**

### Phase 2 — Pages (apply tokens, swap icons, tighten type/spacing — behavior frozen)
- [ ] P1 Dashboard — stat cards, recommended-next card, top-senders list, empty state, quick-action tiles (replace emoji tiles with Lucide).
- [ ] P2 Clean Up (`stats.html` + `stats_table.html`) — premium data table (tabular-nums, hairline rows, hover, sticky header), refined sort/scope/age controls, selection + bulk action bar. Preserve `hx-get`/`action`.
- [ ] P3 Purge preview — confirm/cancel panel as danger-aware card.
- [ ] P4 Triage — AI-required state, streaming result rows as tidy cards/list.
- [ ] P5 Agents — create form, agent rows, soul/features/categories editors, daemon badge, compose form.
- [ ] P6 Watch — start/stop + live status.
- [ ] P7 Sync — options form + progress.
- [ ] P8 Accounts — account list, switch, remove.
- [ ] P9 Add account — Gmail + IMAP flows.
- [ ] P10 Onboarding — multi-step wizard with progress indicator (visual only).
- [ ] P11 Settings — sectioned cards (AI Mode / Chat / Super Agent / Provider / Data / Protected), refined inputs + toggles.
- [ ] P12 Protected senders — add/remove list.
- [ ] P13 Undo history — reversible-action list + undo buttons.
- [ ] P14 Super Agent — header, example chips, conversation, composer, inline result cards.
- [ ] P15 Agent action preview — confirm panel.
- [ ] P16/P17 Chat reply markup, error/blocked/stats-error states.

### Phase 3 — Polish & sweep
- [ ] Grep the whole `templates/` tree for leftover `slate-`/`teal-` literals and any remaining emoji; migrate to tokens/Lucide.
- [ ] Consistent focus rings, hover states, and `:disabled` styling across all buttons/inputs.
- [ ] Verify responsive behavior at the app's normal window sizes (sidebar + content).

---

## 5. Post-Lift Verification Checklist (for the independent verification agent)

> **Task for a separate agent after the lift:** confirm that *every page and every piece of functionality below still exists and works.* Load each route, exercise each interaction, and check the box only on confirmed-working. Report any missing/broken item with the route and template.

**Routes resolve & render (no 500s):** `/`, `/stats`, `/stats/data`, `/purge/preview`, `/purge/confirm`, `/triage`, `/triage/classify-stream`, `/agents`, `/agents/daemon-badge`, `/agents/compose`, `/watch`, `/watch/status`, `/sync`, `/sync/poll/{id}`, `/accounts`, `/accounts/add`, `/accounts/add/gmail/*`, `/accounts/add/imap`, `/onboarding`, `/settings`, `/settings/blocked`, `/undo`, `/agent`, `/agent/stream`, `/agent/action/preview`, `/agent/action/confirm`, `/chat`.

**Per-page functional verification:**
- [ ] **Shell** — sidebar nav links all navigate; active state correct; AI-mode badge shows; account switcher submits `/accounts/switch`; floating assistant opens, sends to `/chat`, renders reply + actions, clear/close work.
- [ ] **P1 Dashboard** — stat cards show real numbers; recommended-next links to Clean Up; top-senders list populates; empty/unauth states render; all 5 quick actions link correctly.
- [ ] **P2 Clean Up** — table loads via htmx; sort, scope, age filters all change results; sender selection works; bulk **purge** and **archive** post to `/purge/preview`.
- [ ] **P3 Purge preview** — shows count/size; Confirm purge & Confirm archive post to `/purge/confirm`; cancel returns.
- [ ] **P4 Triage** — AI-required gating shows when AI off; stream classifies and renders categories.
- [ ] **P5 Agents** — create, toggle, delete; soul editor saves; features toggles save; daemon badge polls; compose returns a draft.
- [ ] **P6 Watch** — start, stop, status polling.
- [ ] **P7 Sync** — start sync; progress polls to completion.
- [ ] **P8 Accounts** — list, switch, remove.
- [ ] **P9 Add account** — Gmail OAuth start+poll; IMAP form submits.
- [ ] **P10 Onboarding** — all steps reachable; inbox connect, AI-mode, credential upload all submit.
- [ ] **P11 Settings** — AI-mode, chat, agent forms save; all inputs present (Claude model, Ollama URL, local model); provider/data/protected sections present.
- [ ] **P12 Protected senders** — add and remove a sender.
- [ ] **P13 Undo** — list shows; undo entry posts to `/undo/{id}`.
- [ ] **P14 Super Agent** — example chips fill input; Run streams a response; create_agent / create_rule / action-preview / unsubscribe / send cards render and their confirm buttons post correctly.
- [ ] **P15 Agent action preview** — confirm posts to `/agent/action/confirm`.
- [ ] **P16/P17** — chat replies render; error/blocked/stats-error states still render.

**Regression guardrails the agent should diff:** no removed/renamed form `action`s, `name=` inputs, element `id`s referenced by JS, or `hx-*` attributes versus the pre-lift templates.

---

## 6. Out of Scope (YAGNI)
- No backend/route/logic changes. No new features. No dependency/build-step changes (stay on Tailwind CDN). Full dark-mode theme is a **deferred optional** follow-up, not part of this lift.
