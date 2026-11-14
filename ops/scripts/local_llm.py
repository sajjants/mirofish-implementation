"""JSON-mode calls to the local Ollama model for the wrapper's own analysis steps.

Used by unknown_variants.py, factguard.py and compare.py. Talks to Ollama directly on the
loopback address from ops/config/model.conf (not through the MiroFish metering proxy, so these
calls never count against a run). Proxies are bypassed; non-loopback hosts are refused.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from opsconfig import model_conf

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LLMError(RuntimeError):
    pass


def clean_model_text(content: str) -> str:
    """Mirror MiroFish's cleanup: drop <think> blocks and an outer ```json fence."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content or "").strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


class LocalLLM:
    def __init__(self, host: Optional[str] = None, model: Optional[str] = None, timeout: float = 600.0):
        conf = model_conf()
        self.host = host or conf.get("OLLAMA_HOST", "127.0.0.1:11434")
        self.model = model or conf.get("OLLAMA_MODEL_ALIAS", "mirofish-llm")
        self.base = f"http://{self.host}"
        if urlparse(self.base).hostname not in LOOPBACK_HOSTS:
            raise ValueError(f"refusing non-loopback Ollama host {self.host!r}")
        self.timeout = timeout
        self.calls = 0

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"POST {path}: {exc}") from None

    def chat_json(self, system: str, user: str, *, validate: Callable[[Dict], Optional[str]],
                  seed: int = 0, temperature: float = 0.2, retries: int = 3, max_tokens: int = 2048) -> Dict:
        """Ask for one JSON object; validate(obj) returns an error string or None. Retries with a new seed."""
        last_error = "no attempt"
        for attempt in range(retries):
            self.calls += 1
            response = self._post("/v1/chat/completions", {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": temperature,
                "seed": seed + attempt,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            })
            try:
                content = response["choices"][0]["message"]["content"]
                value = json.loads(clean_model_text(content))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = f"unparseable response: {exc}"
                continue
            if not isinstance(value, dict):
                last_error = "top-level JSON is not an object"
                continue
            last_error = validate(value) or ""
            if not last_error:
                return value
        raise LLMError(f"model gave no valid answer after {retries} attempts: {last_error}")

    def model_info(self) -> Dict[str, Any]:
        """Digests that pin the exact weights: Ollama manifest digest and the GGUF blob sha256."""
        info: Dict[str, Any] = {"name": self.model, "host": self.host}
        try:
            with _OPENER.open(self.base + "/api/tags", timeout=30) as response:
                tags = json.loads(response.read().decode("utf-8"))
            show = self._post("/api/show", {"model": self.model})
        except (urllib.error.URLError, LLMError, OSError, ValueError) as exc:
            info["error"] = str(exc)
            return info
        entry = next((m for m in tags.get("models", []) if m.get("name") in {self.model, f"{self.model}:latest"}), {})
        info["manifest_digest"] = entry.get("digest")
        blob = re.search(r"^FROM\s+\S*sha256-([0-9a-f]{64})\s*$", show.get("modelfile", ""), re.MULTILINE)
        info["gguf_sha256"] = blob.group(1) if blob else None
        info["details"] = show.get("details", {})
        info["parameters"] = show.get("parameters", "")
        return info


def require_list_of_strings(value: Any, low: int, high: int, max_len: int = 400) -> Optional[str]:
    if not isinstance(value, list):
        return "expected a list"
    items = [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if len(items) != len(value):
        return "list contains non-string or empty items"
    if len({i.lower() for i in items}) != len(items):
        return "list contains duplicates"
    if not low <= len(items) <= high:
        return f"expected {low}-{high} items, got {len(items)}"
    if any(len(i) > max_len for i in items):
        return f"an item is longer than {max_len} characters"
    return None


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"


def as_list(value: Any) -> List:
    return value if isinstance(value, list) else []
