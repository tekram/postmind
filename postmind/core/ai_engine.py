"""AI engine — Claude-powered classification, NL→rule translation, and explainability."""

from __future__ import annotations

import json
from dataclasses import dataclass

from postmind.config import get_settings
from postmind.core.gmail_client import Message

# ── Data models ─────────────────────────────────────────────────────────────


@dataclass
class ClassifiedEmail:
    gmail_id: str
    category: str  # e.g. "action_required", "newsletter", "notification", "conversation", "receipt", "spam"
    priority: str  # "high", "medium", "low"
    explanation: str  # One-line human-readable reason
    suggested_action: str  # "reply", "archive", "unsubscribe", "delete", "keep", "delegate"
    requires_reply: bool
    deadline_hint: str  # e.g. "today", "this week", "" — extracted from content


@dataclass
class BulkOperation:
    gmail_query: str  # Gmail search query to find affected messages
    action: str  # "archive", "trash", "label", "mark_read", "unsubscribe"
    action_params: dict  # e.g. {"label_name": "newsletters"}
    explanation: str  # Human-readable description of what will happen
    estimated_count_hint: str  # e.g. "likely hundreds of messages"
    confidence: float  # 0.0–1.0


@dataclass
class NLRule:
    natural_language: str
    gmail_query: str
    action: str
    action_params: dict
    explanation: str
    warnings: list[str]  # Potential issues to warn the user about


CATEGORIES = {
    "action_required": "Requires a decision or response from you",
    "conversation": "Human-to-human exchange requiring no immediate action",
    "newsletter": "Periodic content you subscribed to",
    "notification": "Automated system/app notification",
    "receipt": "Order confirmation, invoice, or financial record",
    "calendar": "Meeting invite or calendar notification",
    "social": "Social network notification",
    "spam": "Unsolicited or irrelevant",
    "other": "Doesn't fit above categories",
}

SYSTEM_PROMPT = """\
You are an expert email classifier and automation assistant for postmind, \
a privacy-first Gmail management tool. You help users understand and organize \
their email with clear, honest explanations. Never invent information not \
present in the email. Be concise and direct.\
"""


# ── Main AI engine ───────────────────────────────────────────────────────────


class AIEngine:
    def __init__(
        self,
        api_key: str | None = None,
        mode: str | None = None,
        cloud_model: str | None = None,
        ollama_model: str | None = None,
    ):
        """Construct an engine. ``mode``/``cloud_model``/``ollama_model`` override
        the global settings — used by the chat assistant so it can run on a
        different backend than the rest of the app."""
        settings = get_settings()
        self._mode = mode or settings.ai_mode

        if self._mode == "cloud" and settings.cloud_provider == "ollama":
            # Cloud Ollama — same transport as local but user explicitly opted in to external calls
            self._mode = "local"
            self._ollama_url = settings.ollama_base_url.rstrip("/")
            self._ollama_model = ollama_model or settings.ollama_model
            self._ollama_headers = (
                {"Authorization": f"Bearer {settings.ollama_api_key}"}
                if settings.ollama_api_key
                else {}
            )

        elif self._mode == "cloud":
            import anthropic

            key = api_key or settings.anthropic_api_key
            if not key:
                raise ValueError(
                    "ANTHROPIC_API_KEY is not set. "
                    "Set it as an environment variable before running this command."
                )
            self._anthropic = anthropic.Anthropic(api_key=key)
            self._cloud_model = cloud_model or settings.ai_model

        elif self._mode == "local":
            self._ollama_url = settings.ollama_base_url.rstrip("/")
            self._ollama_model = ollama_model or settings.ollama_model
            self._ollama_headers = (
                {"Authorization": f"Bearer {settings.ollama_api_key}"}
                if settings.ollama_api_key
                else {}
            )

        else:
            raise ValueError(
                f"AI mode is '{self._mode}'. Enable cloud or local AI mode to use AI features."
            )

        self._max_batch = settings.ai_max_classify_batch

    def _complete(self, system: str, prompt: str, max_tokens: int = 2048) -> str:
        """Send a prompt to the configured backend and return the text response."""
        if self._mode == "cloud":
            response = self._anthropic.messages.create(
                model=self._cloud_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

        # local — Ollama
        import httpx

        resp = httpx.post(
            f"{self._ollama_url}/api/chat",
            headers=self._ollama_headers,
            json={
                "model": self._ollama_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    # ── Classification ───────────────────────────────────────────────────────

    def classify_emails(
        self, messages: list[Message], parallelism: int | None = None
    ) -> list[ClassifiedEmail]:
        """Classify emails. Returns one ClassifiedEmail per message, in order.

        Batches (``ai_max_classify_batch`` emails each) are dispatched to the LLM
        concurrently — up to ``parallelism`` in flight (default
        ``ai_classify_parallelism``) — so wall-clock time scales with the slowest
        batch rather than the sum of all batches.
        """
        chunks = list(_chunks(messages, self._max_batch))
        if not chunks:
            return []
        if len(chunks) == 1:
            return self.classify_batch(chunks[0])

        from concurrent.futures import ThreadPoolExecutor

        workers = parallelism or get_settings().ai_classify_parallelism
        workers = max(1, min(workers, len(chunks)))

        ordered: list[list[ClassifiedEmail]] = [[] for _ in chunks]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit every batch up front so they run concurrently, then collect
            # results in chunk order (.result() blocks but all calls are in flight).
            futures = {pool.submit(self.classify_batch, ch): i for i, ch in enumerate(chunks)}
            for fut, i in futures.items():
                ordered[i] = fut.result()

        results: list[ClassifiedEmail] = []
        for batch in ordered:
            results.extend(batch)
        return results

    def classify_batch(self, messages: list[Message]) -> list[ClassifiedEmail]:
        email_summaries = []
        for i, msg in enumerate(messages):
            email_summaries.append(
                f"EMAIL {i + 1} (id={msg.id}):\n"
                f"From: {msg.headers.from_}\n"
                f"Subject: {msg.headers.subject}\n"
                f"Snippet: {msg.snippet[:300]}"
            )

        prompt = f"""\
Classify the following {len(messages)} emails. For each, provide a JSON object with:
- gmail_id: the email id given
- category: one of {list(CATEGORIES.keys())}
- priority: "high", "medium", or "low"
- explanation: ONE sentence explaining why you classified it this way
- suggested_action: one of "reply", "archive", "unsubscribe", "delete", "keep", "delegate"
- requires_reply: true/false — does this email need a reply from the user?
- deadline_hint: if there's a time constraint, brief hint ("today", "this week", "by Friday"), else ""

Respond with a JSON array of objects, one per email, in the same order. Nothing else.

{chr(10).join(email_summaries)}\
"""
        raw = self._complete(SYSTEM_PROMPT, prompt)

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        parsed: list[dict] = json.loads(raw)
        results = []
        for item, msg in zip(parsed, messages):
            results.append(
                ClassifiedEmail(
                    gmail_id=item.get("gmail_id", msg.id),
                    category=item.get("category", "other"),
                    priority=item.get("priority", "medium"),
                    explanation=item.get("explanation", ""),
                    suggested_action=item.get("suggested_action", "keep"),
                    requires_reply=bool(item.get("requires_reply", False)),
                    deadline_hint=item.get("deadline_hint", ""),
                )
            )
        return results

    # ── Natural language → Gmail query + action ──────────────────────────────

    def translate_rule(self, natural_language: str) -> NLRule:
        """Convert a natural language instruction to a Gmail query + action."""
        prompt = f"""\
Convert the following natural language email rule into a structured automation.

Rule: "{natural_language}"

Respond with a single JSON object with these fields:
- gmail_query: a valid Gmail search query string (using Gmail operators: from:, to:, subject:, \
  older_than:, newer_than:, has:attachment, label:, is:unread, etc.)
- action: one of "archive", "trash", "label", "mark_read", "unsubscribe"
- action_params: dict of action-specific params (for "label": {{"label_name": "..."}}; else {{}})
- explanation: one sentence describing what this rule will do in plain English
- warnings: list of strings — edge cases or potential issues the user should know about
- confidence: float 0.0–1.0 — how confident you are the query accurately captures the intent

Respond with valid JSON only. No markdown.\
"""
        raw = self._complete(SYSTEM_PROMPT, prompt, max_tokens=1024)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        data = json.loads(raw)
        return NLRule(
            natural_language=natural_language,
            gmail_query=data["gmail_query"],
            action=data["action"],
            action_params=data.get("action_params", {}),
            explanation=data.get("explanation", ""),
            warnings=data.get("warnings", []),
        )

    # ── Bulk operation intent parsing ────────────────────────────────────────

    def parse_bulk_intent(self, instruction: str) -> BulkOperation:
        """Parse a one-off bulk operation instruction (not a recurring rule)."""
        prompt = f"""\
Parse the following bulk email operation request into a structured action.

Request: "{instruction}"

Respond with a JSON object:
- gmail_query: Gmail search query to find the affected messages
- action: one of "archive", "trash", "label", "mark_read", "unsubscribe"
- action_params: dict (for "label": {{"label_name": "..."}})
- explanation: plain English description of what will happen (be specific)
- estimated_count_hint: rough estimate of how many emails this might affect ("dozens", "hundreds", etc.)
- confidence: 0.0–1.0

Respond with valid JSON only.\
"""
        raw = self._complete(SYSTEM_PROMPT, prompt, max_tokens=512)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        data = json.loads(raw)
        return BulkOperation(
            gmail_query=data["gmail_query"],
            action=data["action"],
            action_params=data.get("action_params", {}),
            explanation=data.get("explanation", ""),
            estimated_count_hint=data.get("estimated_count_hint", ""),
            confidence=float(data.get("confidence", 0.8)),
        )

    # ── First-run cleanup narration ──────────────────────────────────────────

    def summarize_cleanup_plan(self, digest: list[dict], total_emails: int,
                               total_senders: int) -> dict:
        """Rewrite a cleanup plan's bucket titles/rationales in warm, plain language
        and add a one-line intro.

        ``digest`` is the body-free output of ``sender_stats.cleanup_plan_digest``
        (key/title/senders/emails/size_mb/action per bucket). The model only
        rephrases text and may only reference the bucket ``key``s it was given — it
        never sees or sets sender lists or numbers, so the caller can safely apply
        the returned text onto the server-side plan without trusting any figures.

        Returns ``{"intro": str, "buckets": {key: {"title": str, "rationale": str}}}``.
        Bucket keys not present in ``digest`` are dropped by the caller."""
        if not digest:
            return {"intro": "", "buckets": {}}

        valid_keys = {b["key"] for b in digest}
        prompt = f"""\
A user just connected an inbox with {total_emails:,} emails from {total_senders:,} \
senders. We analyzed it and grouped the safely-cleanable mail into these buckets \
(numbers already computed — do NOT change them, do NOT invent senders):

{json.dumps(digest, indent=2)}

Write encouraging, plain-language copy that makes cleaning up feel easy and safe. \
Respond with a single JSON object:
- intro: one short sentence summarizing the overall opportunity (mention the scale).
- buckets: an object keyed by each bucket's "key" (only use these keys: \
{sorted(valid_keys)}). Each value is an object with:
  - title: a short, friendly bucket title (max ~6 words)
  - rationale: one short sentence on why it's safe to {{action}} these

Keep it honest and concrete. Never claim emails will be deleted permanently — \
everything is reversible. Respond with valid JSON only. No markdown.\
"""
        raw = self._complete(SYSTEM_PROMPT, prompt, max_tokens=768)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        data = json.loads(raw)
        buckets_in = data.get("buckets", {}) or {}
        buckets: dict[str, dict] = {}
        for key, val in buckets_in.items():
            if key in valid_keys and isinstance(val, dict):
                entry = {}
                if isinstance(val.get("title"), str) and val["title"].strip():
                    entry["title"] = val["title"].strip()
                if isinstance(val.get("rationale"), str) and val["rationale"].strip():
                    entry["rationale"] = val["rationale"].strip()
                if entry:
                    buckets[key] = entry
        intro = data.get("intro", "")
        return {"intro": intro if isinstance(intro, str) else "", "buckets": buckets}

    # ── Weekly digest summary ────────────────────────────────────────────────

    def generate_digest(
        self,
        inbox_summary: dict,
        follow_ups: list[dict],
        avoided_count: int,
        top_senders: list[dict],
    ) -> str:
        """Generate a plain-English weekly digest summary."""
        prompt = f"""\
Generate a concise, useful weekly email digest for the user. Be direct and actionable.

Inbox stats: {json.dumps(inbox_summary)}
Overdue follow-ups: {json.dumps(follow_ups)}
Emails being avoided (viewed but not acted on): {avoided_count}
Top senders by volume this week: {json.dumps(top_senders)}

Write a brief (under 200 words) digest in plain text. Structure:
1. One-line overview
2. Action items (follow-ups that need attention)
3. Quick win (1 cleanup suggestion based on the data)

Be honest and helpful, not cheerful or corporate.\
"""
        return self._complete(SYSTEM_PROMPT, prompt, max_tokens=512)

    # ── Daily brief ──────────────────────────────────────────────────────────

    def generate_daily_brief(
        self,
        today: str,
        unread_count: int,
        new_since_yesterday: int,
        high_priority_items: list[dict],
        overdue_follow_ups: list[dict],
        avoided_count: int,
        recent_unread: list[dict] | None = None,
    ) -> str:
        """Generate a morning brief. Privacy-first: sender+subject only, no bodies.

        Output is Markdown (bold section labels + bullet lists), rendered by the UI.
        """
        recent_unread = recent_unread or []

        # Deterministic, always-accurate status line. We compute this ourselves
        # rather than trust the model — small local models routinely contradict
        # the numbers (e.g. claiming "inbox clear" with 456 unread). The model
        # only writes the narrative below it.
        parts = [f"{unread_count} unread"]
        if new_since_yesterday:
            parts.append(f"{new_since_yesterday} new since yesterday")
        if high_priority_items:
            parts.append(f"{len(high_priority_items)} high-priority")
        if overdue_follow_ups:
            parts.append(f"{len(overdue_follow_ups)} overdue follow-up"
                         f"{'s' if len(overdue_follow_ups) != 1 else ''}")
        status_line = "**Inbox:** " + ", ".join(parts) + "."

        # The attention list is grounded in real emails (high-priority first,
        # else most-recent unread) so it never depends on the model inventing
        # senders or subjects.
        attention = high_priority_items or recent_unread

        prompt = f"""\
Write the body of a morning email brief for {today} in GitHub-flavored Markdown.

These emails need attention (sender + subject only — use exactly these, do not \
invent others):
{json.dumps(attention[:8], indent=2, ensure_ascii=False) if attention else "None — inbox is empty."}

Overdue follow-ups:
{json.dumps(overdue_follow_ups[:5], indent=2, ensure_ascii=False) if overdue_follow_ups else "None"}

Emails being avoided (seen 3+ times, not acted on): {avoided_count}

Write exactly two sections, nothing else (no status line — that is added \
separately, and no preamble):

**What needs attention**
- A short bullet per email above (sender — subject), with at most a 6-word note \
on why it matters. If the list is "None", write one line saying the inbox is \
clear instead of a list.

**Quick win**
One specific action doable in under 2 minutes, referencing a real email above \
when possible.

Use `**bold**` for the two section labels and `- ` for bullets. Be direct. No \
corporate cheerfulness, no filler.\
"""
        body = self._complete(SYSTEM_PROMPT, prompt, max_tokens=600)
        return f"{status_line}\n\n{body.strip()}"

    # ── Avoidance analysis ───────────────────────────────────────────────────

    def analyze_avoided_email(self, msg: Message) -> str:
        """Suggest why the user is avoiding this email and what to do."""
        prompt = f"""\
The user has viewed this email multiple times but hasn't replied, archived, or deleted it.
Suggest in 1-2 sentences why they might be avoiding it and one concrete action they could take.

From: {msg.headers.from_}
Subject: {msg.headers.subject}
Snippet: {msg.snippet[:400]}

Be direct and empathetic. No filler words.\
"""
        return self._complete(SYSTEM_PROMPT, prompt, max_tokens=150)

    # ── Soul-aware email composition ─────────────────────────────────────────

    def compose_email(
        self,
        intent: str,
        recipient_context: str = "",
        thread_snippet: str = "",
        soul: dict | None = None,
    ) -> str:
        """Draft an email in the agent's configured voice.

        Returns a plain-text draft with a Subject line followed by the body.
        Requires cloud (Anthropic) mode — raises ValueError for local-only setups.
        """
        if self._mode != "cloud":
            raise ValueError(
                "Email composition requires cloud AI mode (Anthropic). "
                "Set POSTMIND_AI_MODE=cloud and provide ANTHROPIC_API_KEY."
            )

        soul = soul or {}
        voice_style = soul.get("voice_style") or "professional"
        user_context = soul.get("user_context") or ""
        writing_guidelines = soul.get("writing_guidelines") or ""

        soul_block = f"Voice style: {voice_style}."
        if user_context:
            soul_block += f"\nAbout the sender: {user_context}"
        if writing_guidelines:
            soul_block += f"\nWriting guidelines: {writing_guidelines}"

        system = f"""\
You are a personal email ghostwriter. Write emails exactly as the sender would — \
matching their stated voice, style, and context. Never add filler phrases like \
"I hope this email finds you well." Never explain what you're doing. Output only \
the email itself: first a Subject line (prefixed "Subject: "), then a blank line, \
then the body. No preamble, no sign-off commentary.

{soul_block}\
"""
        parts = [f"Write an email with this intent: {intent}"]
        if recipient_context:
            parts.append(f"Recipient context: {recipient_context}")
        if thread_snippet:
            parts.append(f"Replying to:\n---\n{thread_snippet[:800]}\n---")

        return self._complete(system, "\n\n".join(parts), max_tokens=600)

    # ── Conversational assistant ─────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        tool_executor=None,
        max_tokens: int = 1024,
        max_tool_iterations: int = 6,
    ) -> str:
        """Run a multi-turn assistant conversation and return the final reply text.

        ``messages`` is a list of ``{"role": "user"|"assistant", "content": str}``.
        In cloud mode the model can call ``tools`` (Anthropic tool-use); each call
        is dispatched through ``tool_executor(name, input) -> str``. In local mode
        the same tools are attempted via Ollama's native tool-use; if the model
        can't tool-call (or anything fails) it degrades to plain conversation
        grounded only in ``system`` context.
        """
        if self._mode == "cloud":
            return self._chat_cloud(
                messages, system, tools, tool_executor, max_tokens, max_tool_iterations
            )
        if self._mode == "local":
            # Newer Ollama models (qwen2.5, llama3.1+) support native tool-use. Try
            # it when tools are provided; on any failure (model can't, network,
            # parse) fall back to plain conversation so the assistant still works.
            if tools and tool_executor:
                try:
                    return self._chat_local_tools(
                        messages, system, tools, tool_executor, max_tokens, max_tool_iterations
                    )
                except Exception:
                    pass
            transcript = "\n".join(
                f"{m['role'].capitalize()}: {m['content']}" for m in messages
            )
            prompt = (
                f"{transcript}\n\nAssistant: "
                if transcript
                else "Greet the user briefly and ask how you can help with their inbox."
            )
            return self._complete(system, prompt, max_tokens=max_tokens)
        raise ValueError(
            f"AI mode is '{self._mode}'. Enable cloud or local AI mode to chat."
        )

    def _chat_local_tools(
        self, messages, system, tools, tool_executor, max_tokens, max_tool_iterations
    ) -> str:
        """Tool-use loop against Ollama's /api/chat ``tools`` API.

        Converts our Anthropic-style tool schemas to Ollama's function format,
        dispatches ``tool_calls`` through ``tool_executor``, and loops. Raises on
        any transport/protocol error so :meth:`chat` can fall back to plain
        conversation (Ollama tool-use support varies by model).
        """
        import httpx

        ollama_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]
        convo = [{"role": "system", "content": system}]
        convo += [{"role": m["role"], "content": m["content"]} for m in messages]

        for _ in range(max_tool_iterations):
            resp = httpx.post(
                f"{self._ollama_url}/api/chat",
                headers=self._ollama_headers,
                json={"model": self._ollama_model, "messages": convo, "tools": ollama_tools, "stream": False},
                timeout=180.0,
            )
            resp.raise_for_status()
            msg = resp.json()["message"]
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return (msg.get("content") or "").strip()

            convo.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                try:
                    result = tool_executor(name, args) if tool_executor else "Tool unavailable."
                except Exception as exc:
                    result = f"Error running {name}: {exc}"
                convo.append({"role": "tool", "content": str(result)})

        return "I wasn't able to finish that — could you rephrase or break it into smaller steps?"

    def _chat_cloud(
        self, messages, system, tools, tool_executor, max_tokens, max_tool_iterations
    ) -> str:
        # Cache the (large, reused) system prompt and tool definitions across turns.
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        cached_tools = None
        if tools:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

        convo = [{"role": m["role"], "content": m["content"]} for m in messages]

        for _ in range(max_tool_iterations):
            kwargs = {
                "model": self._cloud_model,
                "max_tokens": max_tokens,
                "system": system_blocks,
                "messages": convo,
            }
            if cached_tools:
                kwargs["tools"] = cached_tools

            resp = self._anthropic.messages.create(**kwargs)

            if resp.stop_reason == "tool_use":
                convo.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    try:
                        result = (
                            tool_executor(block.name, block.input)
                            if tool_executor
                            else "Tool unavailable."
                        )
                    except Exception as exc:  # surface failures to the model, don't crash
                        result = f"Error running {block.name}: {exc}"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        }
                    )
                convo.append({"role": "user", "content": tool_results})
                continue

            return "".join(b.text for b in resp.content if b.type == "text").strip()

        return "I wasn't able to finish that — could you rephrase or break it into smaller steps?"

    # ── Streaming assistant (cloud only) ──────────────────────────────────────

    def chat_stream(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        tool_executor=None,
        max_tokens: int = 1024,
        max_tool_iterations: int = 6,
    ):
        """Stream a multi-turn tool-use conversation as structured events.

        This is the streaming sibling of :meth:`chat`. It is **cloud-only** —
        Ollama tool-use streaming is unreliable, so callers should fall back to a
        single non-streaming call in local mode.

        Yields dicts with a ``"type"`` discriminator:
          - ``{"type": "text_delta", "text": str}`` — a chunk of assistant text.
          - ``{"type": "tool_start", "name": str, "input": dict}`` — a tool is
            about to run (its input JSON has been fully accumulated).
          - ``{"type": "tool_result", "name": str, "summary": str}`` — the tool
            ran; ``summary`` is the (truncated) string the model will see.
          - ``{"type": "done"}`` — the loop finished (final assistant turn or the
            iteration cap was hit).

        Mirrors :meth:`_chat_cloud`: same prompt caching on the system prompt and
        the last tool, same iteration cap, same assistant-tool_use → user-tool_result
        message shape.
        """
        if self._mode != "cloud":
            raise ValueError(
                "chat_stream requires cloud AI mode (Anthropic). "
                "Use chat() for local/off mode."
            )

        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        cached_tools = None
        if tools:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

        convo = [{"role": m["role"], "content": m["content"]} for m in messages]

        for _ in range(max_tool_iterations):
            kwargs = {
                "model": self._cloud_model,
                "max_tokens": max_tokens,
                "system": system_blocks,
                "messages": convo,
            }
            if cached_tools:
                kwargs["tools"] = cached_tools

            # Per-iteration accumulation of streamed content blocks.
            # index -> {"type": "text"|"tool_use", "text": str, "name", "id", "json": str}
            blocks: dict[int, dict] = {}

            with self._anthropic.messages.stream(**kwargs) as stream:
                for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = event.content_block
                        btype = getattr(block, "type", None)
                        if btype == "text":
                            blocks[event.index] = {"type": "text", "text": ""}
                        elif btype == "tool_use":
                            blocks[event.index] = {
                                "type": "tool_use",
                                "name": getattr(block, "name", ""),
                                "id": getattr(block, "id", ""),
                                "json": "",
                            }
                    elif etype == "content_block_delta":
                        delta = event.delta
                        dtype = getattr(delta, "type", None)
                        cur = blocks.get(event.index)
                        if cur is None:
                            continue
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            cur["text"] += text
                            if text:
                                yield {"type": "text_delta", "text": text}
                        elif dtype == "input_json_delta":
                            cur["json"] += getattr(delta, "partial_json", "") or ""

                # Final accumulated message (authoritative content + stop_reason).
                final = stream.get_final_message()

            if final.stop_reason != "tool_use":
                yield {"type": "done"}
                return

            # Append the assistant turn verbatim, then run each tool and build the
            # tool_result user turn — same shape as _chat_cloud.
            convo.append({"role": "assistant", "content": final.content})
            tool_results = []
            for block in final.content:
                if block.type != "tool_use":
                    continue
                yield {"type": "tool_start", "name": block.name, "input": block.input}
                try:
                    result = (
                        tool_executor(block.name, block.input)
                        if tool_executor
                        else "Tool unavailable."
                    )
                except Exception as exc:  # surface failures to the model, don't crash
                    result = f"Error running {block.name}: {exc}"
                result = str(result)
                yield {
                    "type": "tool_result",
                    "name": block.name,
                    "summary": result[:500],
                }
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )
            convo.append({"role": "user", "content": tool_results})

        # Iteration cap hit.
        yield {
            "type": "text_delta",
            "text": "I wasn't able to finish that — could you rephrase or break it into smaller steps?",
        }
        yield {"type": "done"}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _chunks(lst: list, n: int) -> list:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
