"""Tests for the autodraft reply pipeline.

Covered:
  * RFC-2822 in-thread header construction (pure, no network)
  * draft text splitting + Re: subject normalization
  * MockAIEngine.compose_email contract
  * AutodraftService eligibility (should_draft) — triggers + skips
  * draft_reply end-to-end against a fake Gmail provider (+ DraftRecord)
  * run_for_inbox proactive drafting: threshold + per-thread dedupe
  * provider gating (IMAP refuses) and sensitive-sender skip
  * DraftRepo upsert/dedupe semantics
"""

from __future__ import annotations

import base64

import pytest

from postmind.core import autodraft
from postmind.core.autodraft import AutodraftService
from postmind.core.gmail_client import (
    Message,
    MessageHeader,
    _build_raw_message,
    _normalize_message_id,
)
from postmind.core.mock_ai import MockAIEngine
from postmind.core.storage import DraftRecord, DraftRepo, get_session


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── Fakes ─────────────────────────────────────────────────────────────────────


class FakeGmailClient:
    def __init__(self):
        self.drafts: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.deleted: list[str] = []
        self._n = 0

    def create_draft(self, to, subject, body, thread_id=None, in_reply_to=None):
        self._n += 1
        did = f"draft{self._n}"
        self.drafts[did] = {
            "to": to,
            "subject": subject,
            "body": body,
            "thread_id": thread_id,
            "in_reply_to": in_reply_to,
        }
        return did

    def send(self, to, subject, body, thread_id=None, in_reply_to=None):
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "thread_id": thread_id,
                "in_reply_to": in_reply_to,
            }
        )
        return "sentmsg1"

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)


class FakeProvider:
    def __init__(self, messages, supports_drafts=True):
        self._by_id = {m.id: m for m in messages}
        self._ids = [m.id for m in messages]
        self.gmail_client = FakeGmailClient()
        self._supports_drafts = supports_drafts

    def supports(self, capability):
        if capability == "drafts":
            return self._supports_drafts
        return True

    def list_message_ids(self, query="", max_results=None):
        return list(self._ids)

    def get_messages_batch(self, ids):
        return [self._by_id[i] for i in ids if i in self._by_id]


def _msg(id, frm, subject, body, thread_id="t1", message_id="<orig@mail>", list_unsub=""):
    return Message(
        id=id,
        thread_id=thread_id,
        label_ids=["INBOX", "UNREAD"],
        snippet=body[:100],
        headers=MessageHeader(
            subject=subject,
            from_=frm,
            message_id=message_id,
            list_unsubscribe=list_unsub,
        ),
        body_text=body,
    )


def _service(messages, supports_drafts=True):
    provider = FakeProvider(messages, supports_drafts=supports_drafts)
    return AutodraftService(provider, MockAIEngine(), "me@example.com")


# ── RFC-2822 threading headers (pure) ──────────────────────────────────────────


def test_normalize_message_id_wraps_brackets():
    assert _normalize_message_id("abc@mail") == "<abc@mail>"
    assert _normalize_message_id("<abc@mail>") == "<abc@mail>"
    assert _normalize_message_id("") == ""


def test_build_raw_sets_threading_headers():
    raw = _build_raw_message("a@b.com", "Re: hi", "body", in_reply_to="orig@mail")
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "In-Reply-To: <orig@mail>" in decoded
    assert "References: <orig@mail>" in decoded
    assert "Re: hi" in decoded


def test_build_raw_no_threading_when_absent():
    raw = _build_raw_message("a@b.com", "hi", "body")
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "In-Reply-To" not in decoded
    assert "References" not in decoded


# ── Helpers ─────────────────────────────────────────────────────────────────


def test_split_draft():
    subject, body = autodraft._split_draft("Subject: Re: report\n\nHere it is.")
    assert subject == "Re: report"
    assert body == "Here it is."


def test_re_subject_single_prefix():
    assert autodraft._re_subject("report") == "Re: report"
    assert autodraft._re_subject("Re: report") == "Re: report"
    assert autodraft._re_subject("RE: report") == "RE: report"
    assert autodraft._re_subject("") == "Re:"


def test_mock_compose_contract():
    out = MockAIEngine().compose_email("reply", thread_snippet="Can you help?")
    assert out.startswith("Subject: ")
    subject, body = autodraft._split_draft(out)
    assert subject.lower().startswith("re:")
    assert body


# ── Eligibility ───────────────────────────────────────────────────────────────


def test_should_draft_meeting():
    svc = _service([])
    m = _msg("1", "Bob <bob@corp.com>", "Coffee?", "Can we schedule a call next week?")
    eligible, trigger, conf = svc.should_draft(m)
    assert eligible and trigger == "meeting" and conf >= 75


def test_should_draft_request():
    svc = _service([])
    m = _msg("1", "Bob <bob@corp.com>", "Favor", "Could you review this please?")
    eligible, trigger, _ = svc.should_draft(m)
    assert eligible and trigger == "reply_needed"


def test_should_draft_question_mark():
    svc = _service([])
    m = _msg("1", "Bob <bob@corp.com>", "Quick one", "Is the deck done?")
    eligible, trigger, conf = svc.should_draft(m)
    assert eligible and trigger == "reply_needed"


def test_should_draft_skips_newsletter():
    svc = _service([])
    m = _msg("1", "News <news@corp.com>", "Weekly?", "Read more?", list_unsub="<https://unsub>")
    assert svc.should_draft(m)[0] is False


def test_should_draft_skips_noreply():
    svc = _service([])
    m = _msg("1", "no-reply@corp.com", "Receipt?", "Your order shipped. Track?")
    assert svc.should_draft(m)[0] is False


def test_should_draft_skips_sensitive():
    svc = _service([])
    m = _msg("1", "alerts@mybank.com", "Statement?", "Can you confirm this transfer?")
    assert svc.should_draft(m)[0] is False


# ── On-demand draft_reply ───────────────────────────────────────────────────


def test_draft_reply_creates_gmail_draft_and_record():
    m = _msg(
        "g1",
        "Bob <bob@corp.com>",
        "Project",
        "Can you send the report?",
        thread_id="thread9",
        message_id="<bob123@mail>",
    )
    svc = _service([m])
    rec = svc.draft_reply("g1")

    # DraftRecord persisted
    assert rec.id is not None
    assert rec.to_email == "bob@corp.com"
    assert rec.thread_id == "thread9"
    assert rec.in_reply_to_rfc_id == "<bob123@mail>"
    assert rec.subject.lower().startswith("re:")
    assert rec.status == "ready"

    # Gmail draft created in-thread with the right headers
    gc = svc.provider.gmail_client
    assert rec.gmail_draft_id in gc.drafts
    draft = gc.drafts[rec.gmail_draft_id]
    assert draft["to"] == "bob@corp.com"
    assert draft["thread_id"] == "thread9"
    assert draft["in_reply_to"] == "<bob123@mail>"


def test_draft_reply_refuses_sensitive_sender():
    m = _msg("g1", "service@chase-bank.com", "Statement", "Can you confirm?")
    svc = _service([m])
    with pytest.raises(ValueError, match="sensitive"):
        svc.draft_reply("g1")


def test_draft_reply_refuses_without_draft_capability():
    m = _msg("g1", "bob@corp.com", "Hi", "Can you help?")
    svc = _service([m], supports_drafts=False)
    with pytest.raises(ValueError, match="Gmail"):
        svc.draft_reply("g1")


# ── Proactive run_for_inbox ─────────────────────────────────────────────────


def test_run_for_inbox_drafts_high_confidence_only():
    msgs = [
        _msg(
            "a", "Bob <bob@corp.com>", "Call?", "Can we schedule a call?", thread_id="ta"
        ),  # meeting, conf 80 → drafted
        _msg(
            "b", "Ann <ann@corp.com>", "FYI", "Just sharing an update.", thread_id="tb"
        ),  # no signal → skipped
    ]
    svc = _service(msgs)
    created = svc.run_for_inbox(threshold=60)
    assert len(created) == 1
    assert created[0].thread_id == "ta"


def test_run_for_inbox_dedupes_per_thread():
    m = _msg("a", "Bob <bob@corp.com>", "Call?", "Can we schedule a call?", thread_id="ta")
    svc = _service([m])
    first = svc.run_for_inbox()
    assert len(first) == 1
    # Second pass: an open draft already exists for thread ta → no new draft.
    again = svc.run_for_inbox()
    assert again == []
    assert DraftRepo(get_session()).count_open("me@example.com") == 1


# ── DraftRepo ─────────────────────────────────────────────────────────────────


def test_draft_repo_upsert_replaces_open_thread_draft():
    repo = DraftRepo(get_session())
    r1 = repo.upsert_for_thread(
        DraftRecord(
            account_email="me@example.com",
            thread_id="t1",
            to_email="x@y.com",
            subject="Re: a",
            body="one",
            status="ready",
        )
    )
    r2 = repo.upsert_for_thread(
        DraftRecord(
            account_email="me@example.com",
            thread_id="t1",
            to_email="x@y.com",
            subject="Re: a",
            body="two",
            status="ready",
        )
    )
    assert r1.id == r2.id  # same row reused
    assert r2.body == "two"
    assert repo.count_open("me@example.com") == 1


def test_draft_repo_status_transitions():
    repo = DraftRepo(get_session())
    rec = repo.upsert_for_thread(
        DraftRecord(
            account_email="me@example.com",
            thread_id="t1",
            to_email="x@y.com",
            subject="Re: a",
            body="b",
            status="ready",
        )
    )
    repo.set_status(rec.id, "sent")
    assert repo.count_open("me@example.com") == 0
    assert repo.get(rec.id).status == "sent"
