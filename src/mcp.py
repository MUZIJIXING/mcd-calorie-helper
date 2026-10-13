"""Limited read-only client for the official McDonald MCP."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPRedirectHandler, build_opener

URL = "https://mcp.mcd.cn"
ROOT = Path.cwd()
READ_TOOLS = {"list-nutrition-foods", "query-nearby-stores", "query-meals",
              "query-meal-detail", "query-store-coupons", "calculate-price"}


class ProbeError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeError("redirect_refused")


def parse_message(raw, content_type, request_id):
    if "text/event-stream" in content_type:
        messages = []
        normalized = raw.replace("\r\n", "\n")
        for event in normalized.split("\n\n"):
            parts = [line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:")]
            if parts:
                messages.append(json.loads("\n".join(parts)))
    else:
        messages = [json.loads(raw)]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            if "error" in message:
                raise ProbeError("rpc_error")
            if "result" not in message:
                raise ProbeError("missing_result")
            return message["result"]
    raise ProbeError("response_id_missing")


class Client:
    def __init__(self, token, *, opener=None, sleep=time.sleep):
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.session = None
        self.version = "2025-03-26"
        self.next_id = 0
        self.call_metrics = []

    def send(self, method, params=None, *, notification=False):
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self.next_id
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": self.version}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            try:
                request = Request(URL, data=body, headers=headers, method="POST")
                with self.opener.open(request, timeout=15) as response:
                    if response.headers.get("Mcp-Session-Id"):
                        self.session = response.headers["Mcp-Session-Id"]
                    if notification:
                        return None
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise ProbeError("response_too_large")
                    return parse_message(raw.decode("utf-8"), response.headers.get("Content-Type", ""), payload["id"])
            except HTTPError as error:
                if error.code == 401:
                    raise ProbeError("token_invalid_or_expired") from None
                if error.code == 429 and attempt < 2:
                    delay = error.headers.get("Retry-After", "1")
                    try:
                        seconds = float(delay)
                    except ValueError:
                        raise ProbeError("rate_limited_retry_later") from None
                    if not 0 <= seconds <= 5:
                        raise ProbeError("rate_limited_retry_later") from None
                    self.sleep(seconds)
                    continue
                raise ProbeError("rate_limited" if error.code == 429 else "http_error") from None
            except (URLError, TimeoutError, socket.timeout):
                # Do not replay a timed-out request whose completion is unknown.
                raise ProbeError("network_or_tls_error") from None
        raise ProbeError("retry_exhausted")

    def initialize(self):
        result = self.send("initialize", {"protocolVersion": self.version, "capabilities": {},
                                          "clientInfo": {"name": "mcd-calorie-helper", "version": "0.1.0"}})
        version = result.get("protocolVersion")
        if version not in ("2025-03-26", "2025-06-18", "2025-11-25"):
            raise ProbeError("unsupported_protocol_version")
        self.version = version
        self.send("notifications/initialized", notification=True)

    def list_tools(self):
        found, cursor, seen = [], None, set()
        for _ in range(20):
            result = self.send("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result.get("tools"), list):
                raise ProbeError("invalid_tools_schema")
            found.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return [t for t in found if t.get("name") in READ_TOOLS]
            if cursor in seen:
                raise ProbeError("pagination_cycle")
            seen.add(cursor)
        raise ProbeError("pagination_limit")

    def call(self, name, arguments):
        if name not in READ_TOOLS:
            raise ProbeError("write_or_unknown_tool_refused")
        started = time.monotonic()
        status = "transport_error"
        try:
            result = self.send("tools/call", {"name": name, "arguments": arguments})
            body = result.get("structuredContent", {})
            status = "ok" if (not result.get("isError") and body.get("success") is True
                              and body.get("code") == 200) else "business_error"
            return result
        finally:
            self.call_metrics.append({"tool": name, "status": status,
                                      "elapsed_ms": round((time.monotonic() - started) * 1000)})


def scrub(value, token):
    if isinstance(value, dict):
        return {k: scrub(v, token) for k, v in value.items()
                if not (token and token in k)
                and not any(word in k.lower() for word in ("token", "authorization", "cookie", "secret"))}
    if isinstance(value, list):
        return [scrub(v, token) for v in value]
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    return value


def load_token(workspace):
    """Return a private credential and its source label; never include it in output."""
    token = os.environ.get("MCD_MCP_TOKEN")
    source = "environment"
    if not token:
        source = "workspace_file"
        token_file = Path(workspace) / "local/mcd-token.txt"
        if not token_file.exists():
            raise ProbeError("token_not_configured")
        try:
            token = token_file.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError):
            raise ProbeError("token_file_unreadable") from None
    if not token or any(c.isspace() for c in token) or token.lower().startswith("bearer "):
        raise ProbeError("invalid_token_format")
    return token, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", choices=sorted(READ_TOOLS))
    parser.add_argument("--arguments", type=Path, help="JSON object; required when --tool is provided")
    parser.add_argument("--workspace", type=Path, default=ROOT, help="workspace containing private local/ directory")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    response_path = None
    business_code = None
    try:
        token, _ = load_token(workspace)
        if args.tool and not args.arguments:
            raise ProbeError("arguments_file_required")
        arguments = json.loads(args.arguments.read_text(encoding="utf-8")) if args.arguments else None
        if args.tool and not isinstance(arguments, dict):
            raise ProbeError("arguments_must_be_object")
        client = Client(token)
        client.initialize()
        tools = client.list_tools()
        data = {"observed_at": datetime.now(timezone.utc).isoformat(), "protocol_version": client.version,
                "tools": tools}
        output_dir = workspace / "local"
        output_dir.mkdir(exist_ok=True)
        capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        schemas_path = output_dir / ("tool-schemas-" + capture_id + ".json")
        with schemas_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(scrub(data, token), ensure_ascii=False, indent=2))
        if args.tool:
            available = {tool["name"]: tool for tool in tools}
            if args.tool not in available:
                raise ProbeError("tool_unavailable")
            schema = available[args.tool].get("inputSchema", {})
            if any(key not in arguments for key in schema.get("required", [])):
                raise ProbeError("required_arguments_missing")
            result = client.call(args.tool, arguments)
            record = {"observed_at": datetime.now(timezone.utc).isoformat(), "tool": args.tool,
                      "request_arguments": arguments, "result": result}
            response_path = output_dir / f"raw-{args.tool}-{capture_id}.json"
            with response_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(scrub(record, token), ensure_ascii=False, indent=2))
            body = result.get("structuredContent", {})
            if type(body.get("code")) is int:
                business_code = body["code"]
            if result.get("isError") or body.get("success") is not True or body.get("code") != 200:
                raise ProbeError("tool_returned_error")
        print(json.dumps({"status": "ok", "read_tools_found": len(tools), "tool_called": args.tool,
                          "schemas_file": str(schemas_path), "response_file": str(response_path) if response_path else None,
                          "call_metrics": client.call_metrics}, ensure_ascii=False))
        return 0
    except ProbeError as error:
        print(json.dumps({"status": "blocked", "reason": str(error), "business_code": business_code,
                          "response_file": str(response_path) if response_path else None}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"status": "blocked", "reason": "invalid_config_or_response"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
