#!/usr/bin/env python3
"""Loopback metering proxy between MiroFish and Ollama (standard library only).

  llm_meter.py --listen 127.0.0.1:11435 --upstream 127.0.0.1:11434 --log ops/logs/llm_meter.jsonl

start.sh runs it when LLM_BASE_URL in mirofish/.env points at the listen port. Every request is
forwarded unchanged; one JSON line of metadata is appended per request: time, path, model,
HTTP status, latency, token counts, and whether a JSON-mode answer parsed / a tool call had
parseable arguments. Prompt and response text are never written. run_batch.py attributes the
lines to runs and stages by time window.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
MAX_BODY = 64 * 1024 * 1024


def clean_model_text(content: str) -> str:
    """Same cleanup MiroFish applies before json.loads (see local_llm.clean_model_text)."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content or "").strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


def _parses(text: Optional[str]) -> bool:
    try:
        json.loads(clean_model_text(text or ""))
        return True
    except ValueError:
        return False


def analyse(request: Dict[str, Any], response: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Metadata about one chat completion; never includes message content."""
    fmt = (request.get("response_format") or {}).get("type") if isinstance(request.get("response_format"), dict) else None
    record: Dict[str, Any] = {
        "model": request.get("model"),
        "json_mode": fmt in ("json_object", "json_schema"),
        "tools": bool(request.get("tools")),
        "stream": bool(request.get("stream")),
    }
    if not isinstance(response, dict):
        return record
    usage = response.get("usage") or {}
    record["prompt_tokens"] = usage.get("prompt_tokens")
    record["completion_tokens"] = usage.get("completion_tokens")
    try:
        choice = response["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError):
        return record
    record["finish_reason"] = choice.get("finish_reason")
    if record["json_mode"]:
        record["json_ok"] = _parses(message.get("content"))
    if record["tools"]:
        calls = message.get("tool_calls") or []
        record["tool_calls"] = len(calls)
        record["tool_args_ok"] = all(_parses((c.get("function") or {}).get("arguments")) for c in calls) if calls else None
    return record


class MeterServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listen: Tuple[str, int], upstream: Tuple[str, int], log_path: Path, timeout: float):
        super().__init__(listen, MeterHandler)
        self.upstream = upstream
        self.timeout = timeout
        self._lock = threading.Lock()
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(log_path, 0o600)

    def record(self, entry: Dict[str, Any]) -> None:
        line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            os.write(self._fd, line)


class MeterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: MeterServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - keep stderr quiet
        pass

    def _reply(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/meter/health":
            self._reply(200, {"ok": True})
        else:
            self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self._reply(411, {"error": "chunked request bodies are not supported"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._reply(413, {"error": "request too large"})
            self.close_connection = True
            return
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body) if body else {}
            request = request if isinstance(request, dict) else {}
        except ValueError:
            request = {}
        started = time.time()
        entry: Dict[str, Any] = {"ts": round(started, 3), "method": self.command, "path": self.path.split("?")[0]}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
        connection = http.client.HTTPConnection(*self.server.upstream, timeout=self.server.timeout)
        try:
            connection.request(self.command, self.path, body=body or None, headers=headers)
            upstream = connection.getresponse()
            entry["status"] = upstream.status
            if request.get("stream"):
                self._relay_stream(upstream)
                parsed = None
            else:
                data = upstream.read()
                self.send_response(upstream.status)
                for key, value in upstream.getheaders():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                try:
                    parsed = json.loads(data) if upstream.status == 200 else None
                except ValueError:
                    parsed = None
        except (OSError, http.client.HTTPException) as exc:
            entry["status"] = 502
            entry["error"] = type(exc).__name__
            parsed = None
            try:
                self._reply(502, {"error": f"upstream unavailable: {type(exc).__name__}"})
            except OSError:
                pass
        finally:
            connection.close()
        entry["latency_s"] = round(time.time() - started, 3)
        if "completions" in entry["path"]:
            entry.update(analyse(request, parsed))
        self.server.record(entry)

    def _relay_stream(self, upstream: http.client.HTTPResponse) -> None:
        self.send_response(upstream.status)
        for key, value in upstream.getheaders():
            if key.lower() not in HOP_HEADERS:
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            chunk = upstream.read1(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()


def parse_host_port(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    host = host.strip("[]")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"{value!r} is not a loopback address")
    return host, int(port)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen", default="127.0.0.1:11435")
    parser.add_argument("--upstream", default="127.0.0.1:11434")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1800.0)
    args = parser.parse_args(argv)
    server = MeterServer(parse_host_port(args.listen), parse_host_port(args.upstream), args.log, args.timeout)
    print(f"llm_meter listening on {args.listen}, forwarding to {args.upstream}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
