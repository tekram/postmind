# Plan: Local Draft Storage for IMAP Accounts

## Overview
Extend autodraft to support IMAP accounts by storing drafts locally in postmind instead of server-side. Users can review drafts in the `/drafts` UI and send via mailto: links or copy-to-clipboard, maintaining the same UX across both Gmail and IMAP.

## Goals
- IMAP users get the autodraft experience (draft ready to review)
- Drafts live in postmind database for review/edit (never reaches recipient until sent)
- Same `/drafts` UI works for both Gmail and IMAP
- Gmail drafts stay in Gmail (threading preserved), IMAP drafts stay in postmind (no server API)

## Changes Required

### 1. Database Schema Update
**File:** `postmind/core/storage.py`

Add `draft_type` field to `DraftRecord`:
```python
draft_type = Column(String, default="gmail")  # "gmail" | "local"
```

**Migration:** Add column via `_run_migrations()` in the existing migration system.

### 2. Core Logic: AutodraftService
**File:** `postmind/core/autodraft.py`

Update `persist()` method to handle both cases:
```python
def persist(self, context, draft, trigger, confidence, model="") -> DraftRecord:
    if self.supports_drafts():
        # Gmail: create server-side draft
        draft_id = gc.create_draft(...)
        draft_type = "gmail"
    else:
        # IMAP: local draft only
        draft_id = ""
        draft_type = "local"
    
    rec = DraftRecord(
        ...,
        gmail_draft_id=draft_id,
        draft_type=draft_type,
        ...
    )
    return DraftRepo(get_session()).upsert_for_thread(rec)
```

### 3. Web Server: New Endpoints
**File:** `postmind/web/server.py`

Add these endpoints:

**`/drafts/<draft_id>/mailto`** — Generate a mailto: link
```python
@app.get("/drafts/{draft_id}/mailto")
def draft_mailto(draft_id: int):
    """Return a mailto: link for a local draft."""
    draft = DraftRepo(get_session()).get(draft_id)
    if not draft or draft.draft_type != "local":
        raise ValueError("Not a local draft")
    
    # Generate mailto: URL
    mailto_url = _build_mailto_url(
        to=draft.to_email,
        subject=draft.subject,
        body=draft.body,
    )
    return {"mailto_url": mailto_url}
```

**`/drafts/<draft_id>/copy`** — Return draft body for copy-to-clipboard
```python
@app.get("/drafts/{draft_id}/copy")
def draft_copy(draft_id: int):
    """Return draft text formatted for copy-to-clipboard."""
    draft = DraftRepo(get_session()).get(draft_id)
    if not draft:
        raise ValueError("Draft not found")
    
    text = f"To: {draft.to_email}\nSubject: {draft.subject}\n\n{draft.body}"
    return {"text": text, "copied": True}
```

### 4. Utility: mailto: Link Generator
**File:** `postmind/core/autodraft.py`

Add helper function:
```python
def _build_mailto_url(to: str, subject: str, body: str) -> str:
    """Build a mailto: URL with subject and body pre-filled."""
    from urllib.parse import quote
    
    # URL-encode subject and body
    subject_enc = quote(subject)
    body_enc = quote(body)
    
    return f"mailto:{to}?subject={subject_enc}&body={body_enc}"
```

### 5. Template: Updated Drafts UI
**File:** `postmind/web/templates/drafts.html`

Update the draft row to show conditional actions:

**For Gmail drafts (draft_type="gmail"):**
- ✅ Send (existing confirm gate)
- ✏️ Edit & send
- 🗑️ Dismiss

**For local drafts (draft_type="local"):**
- 📧 Open in mailto: (opens email client with draft pre-filled)
- 📋 Copy to clipboard
- ✏️ Edit in postmind
- 🗑️ Dismiss

Add a badge: "💾 local draft" vs "☁️ Gmail draft" to distinguish them.

### 6. Frontend: Copy-to-Clipboard Action
**In `drafts.html`:**
```html
<!-- For local drafts -->
<button hx-get="/drafts/{{ draft.id }}/copy" 
        hx-on="htmx:afterRequest: (el) => navigator.clipboard.writeText(el.detail.xhr.response.text)">
  📋 Copy
</button>
<a href="#" onclick="location.href = '{{ draft.mailto_url }}'; return false;">
  📧 Open in mail client
</a>
```

## Testing

### Unit Tests
- `test_local_draft_creation()` — verify `persist()` creates local drafts for IMAP
- `test_gmail_draft_creation()` — verify `persist()` creates Gmail drafts for Gmail
- `test_mailto_url_generation()` — verify `_build_mailto_url()` handles special characters
- `test_copy_endpoint()` — verify `/drafts/<id>/copy` returns formatted text

### Integration Tests
- Create autodraft on IMAP account → verify `draft_type="local"` and no `gmail_draft_id`
- Create autodraft on Gmail account → verify `draft_type="gmail"` and `gmail_draft_id` set
- Open mailto: link → verify mail client opens with pre-filled fields

## Migration Path

**No breaking changes:**
- Existing Gmail drafts stay the same (default to `draft_type="gmail"`)
- New IMAP drafts get `draft_type="local"`
- `/drafts` page shows both seamlessly

## Security & UX Notes

1. **Mailto: URL length limits** — long bodies (>2KB) may truncate in some mail clients. Provide "Copy to clipboard" as fallback.
2. **No thread info in mailto:** — local drafts appear as new messages, not in-thread. This is a limitation of the IMAP/mailto: approach; the draft record in postmind preserves the thread context for the UI.
3. **Draft lifecycle** — local drafts stay in postmind until user dismisses them (or they expire). No reconciliation with the user's actual sent mail needed.
4. **Privacy** — local drafts never leave the machine; IMAP users get the same privacy as Gmail users.

## Rollout

1. **Phase 1** (now): Add `draft_type` field, update `persist()`, implement mailto: utility
2. **Phase 2**: Add web endpoints, update template with dual action set
3. **Phase 3**: Test on real IMAP + Gmail accounts, gather feedback
