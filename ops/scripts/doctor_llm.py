#!/usr/bin/env python3
"""LLM checks for doctor.sh: model presence, context size, JSON compliance, throughput.

Standard library only. Talks to Ollama on loopback; HTTP proxies are bypassed so that
test prompts can never be routed off-machine by a proxy environment variable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

MIN_NUM_CTX = 16384
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with _OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str, timeout: float) -> dict[str, Any]:
    with _OPENER.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def clean_model_text(content: str) -> str:
    """Mirror MiroFish's cleanup: drop <think> blocks and an outer ```json fence."""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", content).strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return cleaned.strip()


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_ontology(d: dict) -> bool:
    types = d.get("entity_types")
    return (
        isinstance(types, list)
        and len(types) >= 2
        and all(isinstance(t, dict) and _is_str(t.get("name")) and _is_str(t.get("description")) for t in types)
    )


def _check_persona(d: dict) -> bool:
    return (
        _is_str(d.get("bio"))
        and _is_str(d.get("persona"))
        and _is_int(d.get("age"))
        and d.get("gender") in {"male", "female", "other"}
        and isinstance(d.get("mbti"), str)
        and len(d["mbti"]) == 4
    )


def _check_time_config(d: dict) -> bool:
    hours = d.get("total_simulation_hours")
    minutes = d.get("minutes_per_round")
    peaks = d.get("peak_hours")
    return (
        _is_int(hours)
        and 24 <= hours <= 168
        and _is_int(minutes)
        and 30 <= minutes <= 120
        and isinstance(peaks, list)
        and bool(peaks)
        and all(_is_int(h) and 0 <= h <= 23 for h in peaks)
    )


def _check_extraction(d: dict) -> bool:
    entities = d.get("entities")
    relations = d.get("relations")
    if not isinstance(entities, list) or not isinstance(relations, list):
        return False
    names = {e.get("name") for e in entities if isinstance(e, dict)}
    return (
        len(entities) >= 2
        and all(isinstance(e, dict) and _is_str(e.get("name")) and _is_str(e.get("type")) for e in entities)
        and len(relations) >= 1
        and all(
            isinstance(r, dict)
            and r.get("source") in names
            and r.get("target") in names
            and _is_str(r.get("type"))
            and _is_str(r.get("fact"))
            for r in relations
        )
    )


def _check_stance(d: dict) -> bool:
    confidence = d.get("confidence")
    return (
        d.get("stance") in {"supportive", "opposing", "neutral"}
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= confidence <= 1
        and _is_str(d.get("reason"))
    )


JSON_TESTS: list[tuple[str, str, Callable[[dict], bool]]] = [
    (
        "ontology",
        "Text: 'The city council of Riverton voted to close the Elm Street library. "
        "The librarians' union and a parents' group objected.'\n"
        'Return a JSON object {"entity_types": [{"name": str, "description": str}, ...]} '
        "listing at least 3 entity types (PascalCase names) suitable for a social simulation.",
        _check_ontology,
    ),
    (
        "persona",
        "Create a social-media persona for 'a retired schoolteacher in Riverton who uses the "
        'Elm Street library daily\'. Return a JSON object with keys: "bio" (string), '
        '"persona" (string, 2-3 sentences), "age" (integer), "gender" ("male", "female" or '
        '"other"), "mbti" (4-letter string).',
        _check_persona,
    ),
    (
        "time_config",
        "Design the timing of a social-media simulation about a local library closure. Return "
        'a JSON object with keys: "total_simulation_hours" (integer 24-168), '
        '"minutes_per_round" (integer 30-120), "peak_hours" (list of integers 0-23).',
        _check_time_config,
    ),
    (
        "extraction",
        "Text: 'Mayor Dana Ortiz announced on Monday that the Riverton Council will cut the "
        "library budget by 12 percent. Union leader Sam Lee called the plan reckless.'\n"
        'Return a JSON object {"entities": [{"name": str, "type": str}], "relations": '
        '[{"source": str, "target": str, "type": str, "fact": str}]}. Relation source and '
        "target must exactly match entity names.",
        _check_extraction,
    ),
    (
        "stance",
        "Post: 'Closing the library saves money now but costs our kids later. I will be at "
        "the council meeting.'\nClassify the author's stance on the closure. Return a JSON "
        'object with keys: "stance" ("supportive", "opposing" or "neutral"), "confidence" '
        '(number 0-1), "reason" (string).',
        _check_stance,
    ),
]


def run_json_tests(base_url: str, model: str, timeout: float) -> tuple[int, list[dict]]:
    """Send each test prompt in JSON mode (as MiroFish's chat_json does) and validate."""
    results = []
    passed = 0
    for name, prompt, check in JSON_TESTS:
        started = time.monotonic()
        outcome = {"name": name, "passed": False, "error": None, "seconds": None}
        try:
            response = _post_json(
                f"{base_url.rstrip('/')}/chat/completions",
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a precise assistant. Reply with a single JSON object only."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1024,
                    "response_format": {"type": "json_object"},
                },
                timeout,
            )
            choice = response["choices"][0]
            if choice.get("finish_reason") not in (None, "stop"):
                raise ValueError(f"finish_reason={choice.get('finish_reason')}")
            parsed = json.loads(clean_model_text(choice["message"]["content"] or ""))
            if not isinstance(parsed, dict):
                raise ValueError("top-level JSON is not an object")
            if not check(parsed):
                raise ValueError("JSON parsed but failed schema check")
            outcome["passed"] = True
            passed += 1
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
            outcome["error"] = f"{type(error).__name__}: {error}"
        outcome["seconds"] = round(time.monotonic() - started, 2)
        results.append(outcome)
    return passed, results


def run_benchmark(native_url: str, model: str, timeout: float, calls: int = 3) -> dict[str, float]:
    """Measure generation and prompt-processing tokens/sec from Ollama's own counters."""
    gen_rates, prompt_rates, wall = [], [], []
    for i in range(calls):
        started = time.monotonic()
        response = _post_json(
            f"{native_url.rstrip('/')}/api/chat",
            {
                "model": model,
                "stream": False,
                "messages": [
                    {
                        "role": "user",
                        "content": f"(run {i}) In about 150 words, describe how a rumour spreads "
                        "on a social network. Plain prose, no lists.",
                    }
                ],
                "options": {"num_predict": 200, "temperature": 0},
            },
            timeout,
        )
        wall.append(time.monotonic() - started)
        if response.get("eval_duration"):
            gen_rates.append(response["eval_count"] / (response["eval_duration"] / 1e9))
        if response.get("prompt_eval_duration"):
            prompt_rates.append(response["prompt_eval_count"] / (response["prompt_eval_duration"] / 1e9))
    # The first call may include model load time; report the median of all calls.
    median = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0.0  # noqa: E731
    return {
        "gen_tokens_per_sec": round(median(gen_rates), 2),
        "prompt_tokens_per_sec": round(median(prompt_rates), 2),
        "median_call_seconds": round(median(wall), 2),
        "calls": calls,
    }


def parse_num_ctx(parameters: str) -> int | None:
    match = re.search(r"^\s*num_ctx\s+(\d+)\s*$", parameters or "", flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def inspect_model(native_url: str, model: str, timeout: float) -> dict[str, Any] | None:
    tags = _get_json(f"{native_url.rstrip('/')}/api/tags", timeout)
    entry = next(
        (m for m in tags.get("models", []) if m.get("name") in {model, f"{model}:latest"}),
        None,
    )
    if entry is None:
        return None
    show = _post_json(f"{native_url.rstrip('/')}/api/show", {"model": model}, timeout)
    details = show.get("details", {})
    return {
        "name": entry.get("name"),
        "digest": entry.get("digest"),
        "size_bytes": entry.get("size"),
        "num_ctx": parse_num_ctx(show.get("parameters", "")),
        "parameter_size": details.get("parameter_size"),
        "quantization_level": details.get("quantization_level"),
        "family": details.get("family"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Ollama host:port, e.g. 127.0.0.1:11434")
    parser.add_argument("--model", required=True)
    parser.add_argument("--min-pass", type=int, default=4)
    parser.add_argument("--memory-budget-gb", type=float, default=0.0)
    parser.add_argument("--total-memory-gb", type=float, default=0.0)
    parser.add_argument("--profile-out", type=Path)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--skip-bench", action="store_true")
    args = parser.parse_args(argv)

    native_url = f"http://{args.host}"
    hostname = urlparse(native_url).hostname
    if hostname not in LOOPBACK_HOSTS:
        print(f"FAIL  Ollama host '{args.host}' is not loopback; refusing to send test prompts.")
        return 1
    failures = 0

    try:
        info = inspect_model(native_url, args.model, args.timeout)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        print(f"FAIL  could not query Ollama models: {error}")
        return 1
    if info is None:
        print(f"FAIL  model '{args.model}' not found in Ollama. Run ops/scripts/build_model.sh.")
        return 1
    size_gb = (info["size_bytes"] or 0) / 1e9
    print(
        f"PASS  model present: {info['name']} ({info['parameter_size']}, "
        f"{info['quantization_level']}, {size_gb:.1f} GB, digest {str(info['digest'])[:12]})"
    )

    if info["num_ctx"] is None or info["num_ctx"] < MIN_NUM_CTX:
        print(f"FAIL  num_ctx is {info['num_ctx']} (need >= {MIN_NUM_CTX}). Rebuild with build_model.sh.")
        failures += 1
    else:
        print(f"PASS  num_ctx = {info['num_ctx']}")

    if args.memory_budget_gb > 0:
        if size_gb > args.memory_budget_gb * 0.85:
            print(
                f"WARN  model weights {size_gb:.1f} GB leave little room for KV cache within "
                f"~{args.memory_budget_gb:.0f} GB GPU-usable memory"
            )
        else:
            print(f"PASS  model fits GPU-usable memory (~{args.memory_budget_gb:.0f} GB)")

    passed, results = run_json_tests(f"{native_url}/v1", args.model, args.timeout)
    for r in results:
        status = "ok  " if r["passed"] else "FAIL"
        detail = "" if r["passed"] else f"  ({r['error']})"
        print(f"        json[{r['name']}] {status} {r['seconds']}s{detail}")
    total = len(JSON_TESTS)
    if passed >= args.min_pass:
        print(f"PASS  structured JSON compliance {passed}/{total} ({100 * passed // total}%)")
    else:
        print(f"FAIL  structured JSON compliance {passed}/{total} (need >= {args.min_pass})")
        failures += 1

    bench: dict[str, Any] = {}
    if not args.skip_bench:
        try:
            bench = run_benchmark(native_url, args.model, args.timeout)
            print(
                f"INFO  benchmark: {bench['gen_tokens_per_sec']} gen tok/s, "
                f"{bench['prompt_tokens_per_sec']} prompt tok/s, "
                f"median {bench['median_call_seconds']} s/call (3 calls)"
            )
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as error:
            print(f"FAIL  benchmark failed: {error}")
            failures += 1

    if args.profile_out:
        profile = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "platform": sys.platform,
            "total_memory_gb": args.total_memory_gb,
            "gpu_memory_budget_gb": args.memory_budget_gb,
            "model": info,
            "json_compliance": {"passed": passed, "total": total, "results": results},
            "benchmark": bench,
        }
        args.profile_out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.profile_out.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
        print(f"INFO  hardware profile written to {args.profile_out}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
