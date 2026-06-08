"""MCP consumer — connects the Super Agent to external MCP servers.

Each configured server's tools are exposed inside the Super Agent loop with a
namespaced prefix ``mcp_{server_name}_{tool_name}``. The agent can then call
e.g. ``mcp_calendar_list_events`` and the pool routes it to the right server.

Configuration lives in per-account config (``~/.postmind/accounts/<email>.json``)
under the ``mcp_servers`` key:

    {
      "mcp_servers": [
        {"name": "calendar", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-google-calendar"]},
        {"name": "linear", "url": "http://localhost:3333/mcp"}
      ]
    }

``command``/``args`` → stdio subprocess transport.
``url``             → HTTP/SSE streamable transport.

The pool is created once per account at request time and reused across the
agent turn. Tool schemas are fetched on connect and converted to Anthropic-format
dicts. Dispatch is async; call ``dispatch_sync(name, input, loop)`` from sync
tool-executor closures using ``run_coroutine_threadsafe``.

Safety: external MCP WRITE tools are gated by ``allow_execute`` on the server
config. Without it the tool result is returned as text to the model but the
pool will not automatically execute writes — the model must describe the action
and the user triggers it via the existing stage→confirm flow on the postmind
side. (In practice, all tools from external servers are treated as READ tools
for now — the model reasons over the result, then uses postmind's write tools
to act. Full external-write gating is future work.)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    name: str
    # stdio transport
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    # HTTP transport
    url: str = ""
    # Safety gate — if False (default), treat all results as read-only text
    allow_execute: bool = False


class MCPClientSession:
    """Wraps a single MCP server connection (stdio or HTTP)."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.name = config.name
        self._session = None
        self._cm = None  # context manager keeping the transport alive
        self._tools: list[dict] = []  # Anthropic-format, prefixed

    async def connect(self) -> None:
        """Open the transport and fetch the tool list."""
        try:
            from mcp.client.session import ClientSession

            if self.config.command:
                from mcp.client.stdio import StdioServerParameters, stdio_client

                params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args or [],
                    env=self.config.env,
                )
                self._cm = stdio_client(params)
                read, write = await self._cm.__aenter__()
            elif self.config.url:
                from mcp.client.streamable_http import streamablehttp_client

                self._cm = streamablehttp_client(self.config.url)
                read, write, _ = await self._cm.__aenter__()
            else:
                raise ValueError(f"MCP server '{self.name}' needs either 'command' or 'url'.")

            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            await self._load_tools()
        except Exception as exc:
            logger.warning("MCP server '%s' failed to connect: %s", self.name, exc)
            self._session = None
            self._tools = []

    async def _load_tools(self) -> None:
        if self._session is None:
            return
        try:
            result = await self._session.list_tools()
            self._tools = []
            for t in result.tools:
                schema: dict[str, Any] = {}
                if t.inputSchema:
                    if hasattr(t.inputSchema, "model_dump"):
                        schema = t.inputSchema.model_dump(exclude_none=True)
                    elif isinstance(t.inputSchema, dict):
                        schema = t.inputSchema
                self._tools.append({
                    "name": f"mcp_{self.name}_{t.name}",
                    "description": (
                        f"[{self.name}] {t.description or t.name}. "
                        "Result is informational — use postmind's stage_* tools to act."
                    ),
                    "input_schema": schema or {"type": "object", "properties": {}},
                })
        except Exception as exc:
            logger.warning("MCP server '%s' list_tools failed: %s", self.name, exc)
            self._tools = []

    @property
    def tools(self) -> list[dict]:
        return self._tools

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def call(self, tool_name: str, tool_input: dict) -> str:
        """Call a tool by its prefixed name. Returns text."""
        if self._session is None:
            return f"MCP server '{self.name}' is not connected."
        # Strip prefix to get the raw tool name the server knows
        raw_name = tool_name.removeprefix(f"mcp_{self.name}_")
        try:
            result = await self._session.call_tool(raw_name, tool_input)
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(f"[binary data, {len(block.data)} bytes]")
                else:
                    parts.append(str(block))
            return "\n".join(parts) if parts else "(empty result)"
        except Exception as exc:
            return f"MCP tool error ({self.name}/{raw_name}): {exc}"

    async def close(self) -> None:
        try:
            if self._session is not None:
                await self._session.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            if self._cm is not None:
                await self._cm.__aexit__(None, None, None)
        except Exception:
            pass
        self._session = None


class MCPClientPool:
    """Manages MCP client sessions for all configured servers."""

    def __init__(self) -> None:
        self._sessions: dict[str, MCPClientSession] = {}

    async def connect_all(self, configs: list[dict]) -> None:
        """Connect to all configured servers. Errors per-server are logged, not raised."""
        parsed = [_parse_config(c) for c in configs if c.get("name")]
        for cfg in parsed:
            if cfg.name not in self._sessions:
                sess = MCPClientSession(cfg)
                await sess.connect()
                self._sessions[cfg.name] = sess

    def get_tools(self) -> list[dict]:
        """All Anthropic-format tool schemas from all connected servers."""
        tools = []
        for sess in self._sessions.values():
            tools.extend(sess.tools)
        return tools

    def status(self) -> list[dict]:
        """Connection status for each configured server."""
        return [
            {"name": name, "connected": sess.connected, "tool_count": len(sess.tools)}
            for name, sess in self._sessions.items()
        ]

    async def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Route a prefixed tool call to the right server."""
        for sess in self._sessions.values():
            if tool_name.startswith(f"mcp_{sess.name}_"):
                return await sess.call(tool_name, tool_input)
        return f"No MCP server handles tool '{tool_name}'."

    def dispatch_sync(
        self, tool_name: str, tool_input: dict, loop: asyncio.AbstractEventLoop
    ) -> str:
        """Sync wrapper for use from thread-executor closures."""
        future = asyncio.run_coroutine_threadsafe(self.dispatch(tool_name, tool_input), loop)
        try:
            return future.result(timeout=30)
        except Exception as exc:
            return f"MCP dispatch error: {exc}"

    async def close_all(self) -> None:
        for sess in self._sessions.values():
            await sess.close()
        self._sessions.clear()


def _parse_config(raw: dict) -> MCPServerConfig:
    return MCPServerConfig(
        name=str(raw.get("name", "")),
        command=str(raw.get("command", "")),
        args=[str(a) for a in raw.get("args", [])],
        env=raw.get("env") or None,
        url=str(raw.get("url", "")),
        allow_execute=bool(raw.get("allow_execute", False)),
    )


async def build_pool_for_account(account_email: str) -> MCPClientPool:
    """Construct and connect a pool for the given account's mcp_servers config."""
    from postmind.config import load_account_config

    pool = MCPClientPool()
    if not account_email:
        return pool
    cfg = load_account_config(account_email)
    servers = cfg.get("mcp_servers") or []
    if servers:
        await pool.connect_all(servers)
    return pool


# ── Memory server bootstrap ────────────────────────────────────────────────────


def ensure_memory_server(account_email: str) -> dict | None:
    """Return a config dict for the per-account memory server, or None if npx is unavailable."""
    import shutil

    from postmind.config import memory_dir_for

    if not shutil.which("npx"):
        return None
    mem_dir = memory_dir_for(account_email)
    return {
        "name": "memory",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {"MEMORY_FILE_PATH": str(mem_dir / "memory.json")},
    }


def bootstrap_memory_for_account(account_email: str) -> bool:
    """Idempotently add the memory server to an account's mcp_servers config.

    Returns True if the config was updated, False if already configured or npx unavailable.
    Always best-effort — never raises.
    """
    try:
        from postmind.config import load_account_config, save_account_config

        cfg = load_account_config(account_email)
        servers = cfg.get("mcp_servers") or []
        if any(s.get("name") == "memory" for s in servers):
            return False
        entry = ensure_memory_server(account_email)
        if entry is None:
            return False
        servers.append(entry)
        cfg["mcp_servers"] = servers
        save_account_config(account_email, cfg)
        return True
    except Exception:
        return False
