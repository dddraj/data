"""MCP server over stdio, so Claude (Code / Desktop) can drive Ads Manager.

The Model Context Protocol transport is newline-delimited JSON-RPC 2.0 on
stdin/stdout. Only the handful of methods a tools-only server needs are
implemented; that keeps the server dependency-free.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Optional, TextIO

from . import __version__
from .graph import DEFAULT_API_VERSION, GraphClient, GraphError
from .tools import AdsTools

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
Controls Meta (Facebook/Instagram) Ads Manager through the Marketing API.
Start with list_ad_accounts to learn account ids and currency. Budgets are in
the currency's smallest unit (cents/paise). Everything is created PAUSED; only
set_status ACTIVE starts spending, so confirm with the user before doing that
or before raising a budget."""


class McpServer:
    def __init__(self, tools: AdsTools) -> None:
        self.tools = tools

    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:  # notification: never answered
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: Optional[str], params: Dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "facebook-ads", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.input_schema,
                        "annotations": {
                            "readOnlyHint": not t.writes,
                            "destructiveHint": t.writes,
                            "openWorldHint": True,
                        },
                    }
                    for t in self.tools.list()
                ]
            }
        if method == "tools/call":
            return self._call_tool(params.get("name"), params.get("arguments"))
        raise _RpcError(-32601, f"method not found: {method}")

    def _call_tool(self, name: str, arguments: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        # Tool failures go back as isError results, not protocol errors, so
        # Claude can read Meta's message and fix the request.
        try:
            data = self.tools.call(name, arguments)
        except (GraphError, TypeError) as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False)}]}

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                response = self.handle(message) if isinstance(message, dict) else None
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_from_env(env: Dict[str, str] = os.environ) -> McpServer:
    max_budget = env.get("FB_ADS_MAX_DAILY_BUDGET")
    client = GraphClient(
        env.get("META_ACCESS_TOKEN", ""),
        app_secret=env.get("META_APP_SECRET") or None,
        api_version=env.get("META_API_VERSION") or DEFAULT_API_VERSION,
    )
    tools = AdsTools(
        client,
        read_only=env.get("FB_ADS_READ_ONLY", "").lower() in ("1", "true", "yes"),
        max_daily_budget=int(max_budget) if max_budget else None,
    )
    return McpServer(tools)


def main() -> None:
    try:
        server = build_from_env()
    except GraphError as exc:
        print(f"facebook-ads MCP: {exc}", file=sys.stderr)
        sys.exit(1)
    server.serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
