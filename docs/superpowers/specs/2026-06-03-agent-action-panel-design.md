# Agent Action Panel — email-level trash review drawer

**Status:** Approved design (2026-06-03)
**Scope (v1):** Trash-only, Gmail-only, live query. Other actions/backends fall back to existing flow.

## Problem

Today the Super Agent stages destructive actions as **sender-level** inline cards
(checkbox per sender). When a user says *"find all newsletters older than 2 years and
delete them"*, they cannot see or approve the **individual emails** before they go to
Trash. We want a richer, actionable review surface that shows the actual matching
messages.

## Solution overview

Add an **Action Panel**: a right-hand slide-out drawer on `/agent` that lists the actual
emails the agent proposes to trash. Default view is **grouped by sender** (count + size),
expandable to **individual emails**. The user checks/unchecks, then clicks
**"Move N to Trash."** The action is reversible and undo-logged — the existing safe path.

## Flow

1. User asks the agent to delete a class of mail (e.g. newsletters older than 2 years).
2. The model calls a new WRITE tool **`stage_trash_query`**. Like all WRITE tools it
   **stages, never executes**.
3. Server executor resolves the query **live** against the provider, caches the resolved
   message set under a `token`, and emits a `trash_review` card.
4. The `/agent` page renders an **"Open review →"** button. Clicking opens the drawer,
   which fetches the resolved emails as JSON.
5. User reviews (grouped/individual toggle), adjusts the selection, confirms.
6. Confirm endpoint writes an **undo log** then trashes the selected IDs. Drawer collapses;
   chat shows a confirmation with an **Undo** link.

## Components

### 1. Tool schema — `core/agent_tools.py`

New entry in `WRITE_TOOLS`:

- `stage_trash_query`
  - `gmail_query` (string, required): Gmail search operators the model composes, e.g.
    `older_than:2y`, `category:promotions`. This is a **search string only** — never IDs.
  - `newsletters_only` (boolean, default false): when true, the server keeps only messages
    that carry a `List-Unsubscribe` header.
  - `description` (string, required): human label for the review, e.g.
    "newsletters older than 2 years".

New stateless helper:

- `resolve_trash_query(provider, gmail_query, newsletters_only, limit) -> list[dict]`
  - Calls `provider.list_message_ids(query=gmail_query, max_results=limit)` then
    `provider.get_messages_metadata(ids)`.
  - If `newsletters_only`, filter to messages whose `headers.list_unsubscribe` is non-empty.
  - Returns dicts: `{id, subject, sender_email, sender_name, date, internal_date,
    size_estimate}` (date is a human string derived from `internal_date`).

### 2. Server — `web/server.py`

**Review cache** (module-level, mirrors the existing scan cache pattern):
`_REVIEW_CACHE: dict[token, {account_email, description, emails: list[dict], created_at}]`
with a TTL sweep. Resolve **once at stage time** and cache the resolved set so the preview
and the confirm see the same messages (no drift) and IDs are guaranteed server-originated.

**Executor branch** (`_build_agent_tool_executor`): handle `stage_trash_query` —
- Build provider; if it does not support a live query backend cleanly, return a message
  steering to the sender-level flow.
- Call `resolve_trash_query`. If empty, return "nothing matched — nothing staged."
- Store resolved emails under a fresh `token` in `_REVIEW_CACHE`.
- Append card `{type: "trash_review", fields: {token, total_count, sender_count,
  description}}`.
- Return a one-line summary to the model (counts only — never the email contents, to keep
  untrusted bodies out of the model loop).

**`GET /agent/review/{token}`** → JSON:
```
{
  "description": str,
  "total_count": int,
  "groups": [ {sender_email, sender_name, count, size_bytes, size_str, sensitive,
               emails: [{id, subject, date, size_str}]} ],
}
```
Sensitive senders (bank/legal/health, via existing detection) are flagged so the UI can
**pre-uncheck** them. 404 if the token is unknown/expired.

**`POST /agent/review/{token}/confirm`** (form: `ids` repeated) →
- Intersect submitted `ids` with the token's cached set. **Reject** (ignore) any id not in
  the set — defends the trust boundary.
- Write an `UndoLogRepo.record(operation="trash", message_ids=..., description=...)`.
- `provider.batch_trash(ids)`.
- Return JSON `{trashed: int, undo_href: "/undo"}`.

### 3. Frontend — `web/templates/agent.html`

- `cardHtml`: add a `trash_review` branch rendering the card title + count and an
  **"Open review →"** button carrying the `token`.
- A **drawer** element (fixed right slide-out, ~`max-w-md`, overlay scrim on mobile). On
  open it `fetch`es `GET /agent/review/{token}` and renders:
  - A **Grouped / Individual** toggle.
  - Grouped: one row per sender (name, email, count, size, sensitive badge), a master
    checkbox, expandable to individual emails.
  - Individual: flat list of emails (subject, sender, date, size), checkbox each.
  - Sticky footer: "N selected · ~X MB" + **Move to Trash** button + Cancel.
- Confirm: `POST` selected ids; on success collapse the drawer and append an assistant
  bubble: "Moved N emails to Trash — undoable for 30 days." with an Undo action link.

## Safety model

- Deletes go to **Trash** via `provider.batch_trash`, never permanent.
- Every confirm writes a **30-day undo log** before trashing (BulkEngine ordering).
- The model supplies only a **search string**; message IDs are **server-resolved**.
  Confirm enforces submitted ⊆ cached — no model- or client-injected IDs can execute.
- Sensitive senders are pre-unchecked.

## Testing (no API key required)

- `resolve_trash_query`: `newsletters_only` keeps only `List-Unsubscribe` messages; passes
  the query through to the provider; maps fields correctly. (fake provider)
- `GET /agent/review/{token}`: groups correctly, flags sensitive, 404 on bad token.
- `POST .../confirm`: writes an undo log, trashes only submitted∩cached IDs, drops foreign
  IDs, returns the count.
- Agent tool path exercised under `MockAIEngine`.

## Out of scope (v1)

- Archive/label/mark-read/unsubscribe via the panel (stay as today's cards).
- Non-Gmail backends (fall back to sender-level flow).
- Persisting a review across page reloads (token is ephemeral, TTL-bounded).
