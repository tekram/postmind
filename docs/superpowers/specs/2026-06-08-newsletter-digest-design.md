# Newsletter & Promotions Digest — Design Spec

**Date:** 2026-06-08  
**Status:** Approved  

## Overview

Extend the Daily Brief with two new tabs — **Newsletters** and **Promotions** — that batch-summarize subscription and promotional emails from the last 24 hours, then auto-trash them 48 hours after digest generation. Users can exempt any sender via a per-card toggle, which persists across future digests.

---

## Goals

- Surface newsletters and promotional emails in a readable daily digest instead of letting them pile up
- Auto-trash non-exempted emails 48h after digest generation (reversible via undo log)
- Let users permanently exempt specific senders from cleanup with a single toggle
- Fold the existing "Deals" tab content into the new Promotions tab

## Non-Goals

- Real-time newsletter detection (digest is once-per-day, same as the existing brief)
- IMAP support (Gmail only for auto-trash; digest display works on both)
- Digest without AI enabled (tabs render but summaries require `ai_mode != "off"`)

---

## Data Model

### `daily_briefs` table — new columns

| Column | Type | Description |
|--------|------|-------------|
| `newsletters_json` | TEXT (JSON) | List of newsletter digest items (see schema below) |
| `promotions_json` | TEXT (JSON) | List of promo digest items (see schema below) |
| `digest_trash_after` | DATETIME | UTC timestamp: `generated_at + 48h`. Nulled after trash executes. |

**Newsletter item schema:**
```json
{
  "sender": "The Rundown AI",
  "sender_email": "hello@therundown.ai",
  "email_ids": ["msg_id_1", "msg_id_2"],
  "summary_bullets": ["bullet 1", "bullet 2", "bullet 3"],
  "exempted": false
}
```

**Promo item schema:**
```json
{
  "sender": "Luma Health",
  "sender_email": "deals@lumahealth.io",
  "email_ids": ["msg_id_3"],
  "offer_line": "30% off annual plans through June 15",
  "exempted": false
}
```

### `DigestExemption` table — new table

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | |
| `account_email` | TEXT | Account this exemption belongs to |
| `sender_email` | TEXT | Sender to never auto-trash |
| `created_at` | DATETIME | |

Unique constraint on `(account_email, sender_email)`.

---

## Generation Pipeline

`DailyBriefGenerator.get_or_generate()` calls two new private methods at the end of the existing generation flow. Both are skipped if `ai_mode == "off"` (columns stay `null`; UI shows enable-AI prompt).

### `_generate_newsletter_digest(session, provider, ai, account_email)`

1. Query `EmailRecord` cache: last 24h, `list_unsubscribe != ""`, `is_inbox=True`
2. Group by `sender_email`
3. Skip any sender present in `DigestExemption` for this account
4. For each sender group: call `summarize_thread` → 3-bullet AI summary
5. Sort by most recent email descending
6. Return list → stored in `newsletters_json`

### `_generate_promo_digest(session, provider, ai, account_email)`

1. Query `EmailRecord` cache: last 24h, `ai_category = "promotion"`, `list_unsubscribe = ""`
2. Group by `sender_email`, skip exempted senders
3. For each sender: send subject + 300-char snippet to AI → extract single offer line
4. Sort promos with `deal_score > 0` first (replaces existing Deals tab)
5. Return list → stored in `promotions_json`

### Trash timestamp

Set `digest_trash_after = now() + 48h` when either digest produces at least one non-empty item.

---

## UI

The Daily Brief page (`/brief`) gains two new tabs after the existing ones.

### Newsletters tab

- One card per sender
- Card: sender name (bold) + email count, then 3 bullet points of summary
- "Keep" toggle on each card — exempts sender (`POST /digest/exempt`), removes trash indicator
- Badge per card: "Trashing in Xh" based on `digest_trash_after`
- Empty state: "No newsletters in the last 24 hours"
- AI-off state: "Enable AI in Settings to generate newsletter summaries" + settings link

### Promotions tab

- One row per sender: sender name, offer line in bold, email count
- "Keep" toggle + "Trashing in Xh" badge (same as newsletters)
- Promos with `deal_score > 0` sorted to top
- Empty state: "No promotional emails in the last 24 hours"
- AI-off state: same enable-AI prompt

### Brief header

When `digest_trash_after` is set and in the future:

> "X newsletters and Y promotions will be trashed in 48h — [Undo All]"

**Undo All:** inserts `DigestExemption` rows for every sender in both digests for this account.

### Removed

The existing **Deals** tab is removed. Its content surfaces in the Promotions tab (sorted first by `deal_score`).

---

## Auto-Trash Execution

New step in daemon `_triage_account`: `_run_digest_trash(session, provider, account_email)`

**Logic:**
1. Load today's `DailyBrief` row for the account
2. If `digest_trash_after` is null or `> now()`: skip
3. Collect all `email_ids` from `newsletters_json` + `promotions_json` where `exempted = False`
4. Re-check `DigestExemption` at execution time — respect toggles flipped after generation
5. Call `provider.trash_messages(email_ids)` in batches of 50
6. Write undo log entry (30-day window, action type `digest_trash`)
7. Set `digest_trash_after = None` — prevents re-execution on subsequent heartbeats
8. If the brief row is from a prior calendar day: skip entirely (no retroactive trashing)

**Idempotent:** nulling `digest_trash_after` after execution means repeated heartbeat runs are safe.

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/digest/exempt` | Body: `{sender_email}`. Inserts `DigestExemption`, sets `exempted=True` on today's brief JSON. |
| `DELETE` | `/digest/exempt` | Body: `{sender_email}`. Removes exemption, sets `exempted=False` on today's brief JSON. |

Both endpoints scope to the active account session.

---

## Error Handling

- If summarization fails for a sender: include the item with `summary_bullets: []` and a note in the UI ("Summary unavailable")
- If `provider.trash_messages` partially fails: log the error, write undo log for successfully trashed IDs, leave `digest_trash_after` null (don't retry automatically — user can undo and regenerate)
- If no emails qualify: digest columns store `[]`, no `digest_trash_after` set

---

## Testing

- `MockAIEngine` returns canned bullets/offer lines — digest generation fully testable without API key
- New `clean_db` fixture tests: digest generation with exempted senders, auto-trash execution timing, idempotency of trash step, undo log written correctly
- Test that Deals tab content surfaces correctly in Promotions tab
- Test `digest_trash_after` is not set when both digests are empty
