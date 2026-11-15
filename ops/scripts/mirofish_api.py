#!/usr/bin/env python3
"""Minimal client for the MiroFish REST API (standard library only, loopback only).

Drives one run end to end: ontology -> graph build -> simulation create -> prepare
-> start -> wait for rounds -> close environment -> report. Each finished stage is
recorded in ops/runs/<RUN_ID>/manifest.json, so re-running the same RUN_ID resumes
after the last completed stage.

  mirofish_api.py health
  mirofish_api.py run --run-id ID --seed FILE [--seed FILE ...] --requirement-file FILE
                      [--max-rounds 10] [--max-agents N]
"""

from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

OPS_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = OPS_DIR / "runs"
SIMULATIONS_DIR = OPS_DIR.parent / "mirofish" / "backend" / "uploads" / "simulations"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Upstream's single-platform scripts (run_reddit_simulation.py, run_twitter_simulation.py) never
# write <platform>/actions.jsonl or its simulation_end event, which is how the backend detects the
# end of the rounds; a 'reddit' or 'twitter' run therefore reports round 0 forever. Only the
# parallel script logs actions, so it is the only mode this wrapper drives.
PLATFORMS = ("parallel",)


class ApiError(RuntimeError):
    """The backend answered with success=false, an HTTP error, or a failed task."""


class PrepareMismatch(ApiError):
    """Prepare finished, but its files do not hold the expected number of agents."""


def prepared_agent_counts(sim_dir: Path) -> Dict[str, Optional[int]]:
    """Agents in each file prepare writes; None for a file that is missing or unreadable."""
    counts: Dict[str, Optional[int]] = {}

    def read(name: str, count: Callable[[Path], int]) -> None:
        try:
            counts[name] = count(sim_dir / name)
        except (OSError, ValueError, KeyError, TypeError, csv.Error):
            counts[name] = None

    def csv_rows(path: Path) -> int:
        with open(path, newline="", encoding="utf-8") as handle:
            return sum(1 for _ in csv.DictReader(handle))

    read("reddit_profiles.json", lambda p: len(json.loads(p.read_text(encoding="utf-8"))))
    read("twitter_profiles.csv", csv_rows)
    read("simulation_config.json", lambda p: len(json.loads(p.read_text(encoding="utf-8"))["agent_configs"]))
    return counts


def default_base_url() -> str:
    """MIROFISH_API_URL, else 127.0.0.1 on the FLASK_PORT configured in mirofish/.env."""
    if os.environ.get("MIROFISH_API_URL"):
        return os.environ["MIROFISH_API_URL"]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from envfile import parse, read_lines

    port = parse(read_lines(OPS_DIR.parent / "mirofish" / ".env")).get("FLASK_PORT") or "5001"
    return f"http://127.0.0.1:{int(port)}"


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id or "") or ".." in run_id:
        raise ValueError(f"invalid run id {run_id!r}: use letters, digits, '_', '-', '.' (max 64)")
    return run_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_private_json(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically write JSON readable only by the current user."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


class MiroFishClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 60.0):
        base_url = base_url or default_base_url()
        host = urllib.parse.urlparse(base_url).hostname
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"refusing non-loopback MiroFish URL: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # An empty ProxyHandler ignores HTTP(S)_PROXY so requests can never leave the machine.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: Optional[bytes] = None,
                 content_type: Optional[str] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("Accept", "application/json")
        request.add_header("Accept-Language", "en")
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ApiError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path} -> {exc.reason}") from None
        if not payload.get("success", False):
            raise ApiError(f"{method} {path} -> {payload.get('error') or payload}")
        return payload.get("data") or {}

    def get(self, path: str, **kwargs) -> Dict[str, Any]:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, payload: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        return self._request("POST", path, json.dumps(payload).encode("utf-8"), "application/json", **kwargs)

    def delete(self, path: str, **kwargs) -> Dict[str, Any]:
        return self._request("DELETE", path, **kwargs)

    def post_multipart(self, path: str, fields: Dict[str, str], files: Iterable[Path], **kwargs) -> Dict[str, Any]:
        boundary = f"mirofish-{uuid.uuid4().hex}"
        parts: List[bytes] = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + value.encode("utf-8") + b"\r\n"
            )
        for file_path in files:
            mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            filename = file_path.name.replace('"', "")
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n".encode() + file_path.read_bytes() + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request("POST", path, b"".join(parts), f"multipart/form-data; boundary={boundary}", **kwargs)

    def health(self) -> bool:
        try:
            with self._opener.open(self.base_url + "/health", timeout=5) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def entity_counts(self, graph_id: str) -> Dict[str, int]:
        """Typed entities in a built graph, by type (each becomes an agent); no LLM call."""
        data = self.get(f"/api/simulation/entities/{urllib.parse.quote(graph_id)}?enrich=false")
        counts: Dict[str, int] = {}
        for entity in data.get("entities") or []:
            labels = [label for label in (entity.get("labels") or []) if label not in ("Entity", "Node")]
            kind = str(labels[0]) if labels else "unknown"
            counts[kind] = counts.get(kind, 0) + 1
        return counts


def poll(fetch: Callable[[], Dict[str, Any]], is_done: Callable[[Dict[str, Any]], bool],
         label: str, timeout_s: float, interval_s: float = 5.0,
         log: Callable[[str], None] = print) -> Dict[str, Any]:
    """Call fetch() until is_done(result) or the timeout; fail fast on a 'failed' status."""
    deadline = time.monotonic() + timeout_s
    last_message = None
    while True:
        state = fetch()
        if str(state.get("status", "")).lower() == "failed" or state.get("runner_status") == "failed":
            raise ApiError(f"{label} failed: {state.get('error') or state.get('message') or state}")
        if is_done(state):
            return state
        message = state.get("message") or state.get("runner_status")
        progress = state.get("progress", state.get("progress_percent"))
        summary = f"{label}: {progress}% {message or ''}".strip()
        if summary != last_message:
            log(summary)
            last_message = summary
        if time.monotonic() > deadline:
            raise ApiError(f"{label} did not finish within {int(timeout_s)}s (last state: {summary})")
        time.sleep(interval_s)


def rounds_done(state: Dict[str, Any], platform: str) -> bool:
    if state.get("runner_status") in ("completed", "stopped"):
        return True
    need_twitter = platform in ("twitter", "parallel")
    need_reddit = platform in ("reddit", "parallel")
    return (not need_twitter or bool(state.get("twitter_completed"))) and \
           (not need_reddit or bool(state.get("reddit_completed")))


class RunPipeline:
    """One MiroFish run with a resumable manifest in ops/runs/<RUN_ID>/."""

    STAGES = ("ontology", "graph", "simulation", "prepare", "schedule", "rounds", "close_env", "report")

    def __init__(self, client: MiroFishClient, run_id: str, runs_dir: Path = RUNS_DIR,
                 log: Callable[[str], None] = print, poll_interval_s: float = 5.0,
                 simulations_dir: Optional[Path] = None):
        """simulations_dir: where upstream writes simulations; given, prepare's agent files are checked."""
        self.client = client
        self.simulations_dir = simulations_dir
        self.run_id = validate_run_id(run_id)
        self.run_dir = runs_dir / run_id
        self.manifest_path = self.run_dir / "manifest.json"
        self.log = log
        self._poll = functools.partial(poll, log=log, interval_s=poll_interval_s)
        self.manifest: Dict[str, Any] = {}
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        write_private_json(self.manifest_path, self.manifest)

    def _stage(self, name: str, action: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        stages = self.manifest.setdefault("stages", {})
        if stages.get(name, {}).get("status") == "done":
            self.log(f"[{name}] already done, skipping")
            return stages[name]["result"]
        started = time.time()
        stages[name] = {"status": "running", "started_at": started}
        self._save()
        try:
            result = action()
        except BaseException as exc:
            stages[name].update(status="failed", error=str(exc), ended_at=time.time())
            self._save()
            raise
        stages[name].update(status="done", result=result, ended_at=time.time(),
                            seconds=round(time.time() - started, 1))
        self._save()
        self.log(f"[{name}] done in {stages[name]['seconds']}s")
        return result

    def run(self, seed_files: List[Path], requirement: str, *, max_rounds: int, platform: str = "parallel",
            max_agents: Optional[int] = None, entity_types: Optional[List[str]] = None,
            enable_graph_memory_update: bool = False, project_name: Optional[str] = None,
            stage_timeout_s: float = 4 * 3600, until: Optional[str] = None,
            stall_timeout_s: float = 1800,
            before_rounds: Optional[Callable[[str], Dict[str, Any]]] = None) -> Dict[str, Any]:
        """before_rounds(simulation_id), if given, runs once as the 'schedule' stage after prepare."""
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {PLATFORMS}: upstream's single-platform scripts never "
                             "report the end of the rounds, so the run would wait forever")
        if not 1 <= int(max_rounds) <= 40:
            raise ValueError("max_rounds must be between 1 and 40 (hard ceiling)")
        inputs = {
            "seed_files": [{"name": p.name, "sha256": sha256_file(p)} for p in seed_files],
            "requirement_sha256": hashlib.sha256(requirement.encode("utf-8")).hexdigest(),
            "max_rounds": int(max_rounds),
            "platform": platform,
            "max_agents": max_agents,
            "entity_types": entity_types,
            "enable_graph_memory_update": enable_graph_memory_update,
        }
        previous = self.manifest.get("inputs")
        if previous:
            def without_types(values: Dict[str, Any]) -> Dict[str, Any]:
                return {k: v for k, v in values.items() if k != "entity_types"}

            if without_types(previous) != without_types(inputs):
                raise ApiError(f"run {self.run_id} exists with different inputs; use a new run id")
            prepare_status = (self.manifest.get("stages", {}).get("prepare") or {}).get("status")
            if previous.get("entity_types") != entity_types and prepare_status in ("running", "done"):
                raise ApiError(f"run {self.run_id}: personas were already generated for entity types "
                               f"{previous.get('entity_types')}; use a new run id to change them")
        self.manifest.setdefault("run_id", self.run_id)
        self.manifest.setdefault("created_at", time.time())
        self.manifest["inputs"] = inputs
        for directory in (self.run_dir.parent, self.run_dir):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        self._save()
        c = self.client

        def ontology():
            data = c.post_multipart("/api/graph/ontology/generate",
                                    {"simulation_requirement": requirement,
                                     "project_name": project_name or self.run_id},
                                    seed_files, timeout=1800)
            return {"project_id": data["project_id"]}

        project_id = self._stage("ontology", ontology)["project_id"]
        self.manifest["project_id"] = project_id

        def graph():
            task_id = c.post("/api/graph/build", {"project_id": project_id})["task_id"]
            state = self._poll(lambda: c.get(f"/api/graph/task/{task_id}"),
                         lambda s: s.get("status") == "completed", "graph build", stage_timeout_s)
            return dict(state.get("result") or {})

        graph_result = self._stage("graph", graph)
        graph_id = graph_result["graph_id"]
        self.manifest["graph_id"] = graph_id

        def simulation():
            data = c.post("/api/simulation/create", {
                "project_id": project_id, "graph_id": graph_id,
                "enable_twitter": platform in ("twitter", "parallel"),
                "enable_reddit": platform in ("reddit", "parallel"),
            })
            return {"simulation_id": data["simulation_id"]}

        simulation_id = self._stage("simulation", simulation)["simulation_id"]
        self.manifest["simulation_id"] = simulation_id

        def prepare():
            payload: Dict[str, Any] = {"simulation_id": simulation_id, "use_llm_for_profiles": True}
            if entity_types:
                # Every run generates its own ontology, so type names chosen in advance may not exist.
                graph_counts = c.entity_counts(graph_id)
                missing = [kind for kind in entity_types if kind not in graph_counts]
                if missing:
                    listing = ", ".join(f"{kind} {n}" for kind, n in
                                        sorted(graph_counts.items(), key=lambda kv: -kv[1]))
                    raise ApiError(f"entity type(s) {', '.join(missing)} are not in this run's graph. Entities by "
                                   f"type: {listing}. Run the same command again with --entity-type options "
                                   "chosen from these types, or with none to make every entity an agent.")
                payload["entity_types"] = entity_types
            if self.manifest.pop("prepare_mismatch", False):
                # Upstream would otherwise return the mismatched files as already prepared.
                payload["force_regenerate"] = True
            data = c.post("/api/simulation/prepare", payload, timeout=300)
            agents = data.get("expected_entities_count")
            if max_agents is not None and agents is not None and agents > max_agents:
                self.log(f"WARNING: {agents} agents exceeds the cap of {max_agents}")
            if not data.get("already_prepared"):
                task_id = data.get("task_id")
                self._poll(lambda: c.post("/api/simulation/prepare/status",
                                    {"task_id": task_id, "simulation_id": simulation_id}),
                     lambda s: s.get("status") in ("completed", "ready"), "prepare", stage_timeout_s)
            else:
                agents = (data.get("prepare_info") or {}).get("entities_count")
            result: Dict[str, Any] = {"expected_entities_count": agents}
            if self.simulations_dir is not None:
                counts = prepared_agent_counts(self.simulations_dir / simulation_id)
                if not agents or any(n != agents for n in counts.values()):
                    self.manifest["prepare_mismatch"] = True
                    found = ", ".join(f"{name} {'missing' if n is None else n}" for name, n in counts.items())
                    raise PrepareMismatch(
                        f"prepare wrote the wrong number of agents: expected {agents}, found {found}. Something "
                        "else (for example the MiroFish web page) may have started a prepare for this "
                        "simulation. Run the same command again to regenerate the personas.")
                result["agent_files"] = counts
            return result

        self._stage("prepare", prepare)
        if until == "prepare":
            return self.manifest
        if before_rounds is not None:
            self._stage("schedule", lambda: before_rounds(simulation_id))

        def rounds():
            status_path = f"/api/simulation/{simulation_id}/run-status"
            current = c.get(status_path).get("runner_status")
            if current in ("starting", "running", "paused", "completed"):
                self.log(f"rounds: simulation already {current}; not restarting it")
            else:
                # force=True makes upstream discard a stopped/failed attempt and start clean.
                c.post("/api/simulation/start", {
                    "simulation_id": simulation_id, "platform": platform, "max_rounds": int(max_rounds),
                    "enable_graph_memory_update": enable_graph_memory_update,
                    "force": current in ("stopped", "failed"),
                }, timeout=120)
            progress = {"key": None, "since": time.monotonic()}

            def fetch_with_stall_check() -> Dict[str, Any]:
                state = c.get(status_path)
                key = (state.get("current_round"), state.get("total_actions_count"), state.get("runner_status"))
                if key != progress["key"]:
                    progress.update(key=key, since=time.monotonic())
                elif not rounds_done(state, platform) and time.monotonic() - progress["since"] > stall_timeout_s:
                    raise ApiError(f"rounds: no progress for {int(stall_timeout_s)}s (round {key[0]}, "
                                   f"{key[1]} actions, {key[2]}); check the simulation directory's logs")
                return state

            state = self._poll(fetch_with_stall_check, lambda s: rounds_done(s, platform), "rounds", stage_timeout_s)
            keys = ("current_round", "total_rounds", "twitter_actions_count", "reddit_actions_count",
                    "total_actions_count", "runner_status", "started_at")
            return {k: state.get(k) for k in keys}

        self._stage("rounds", rounds)

        def close_env():
            try:
                c.post("/api/simulation/close-env", {"simulation_id": simulation_id, "timeout": 60}, timeout=120)
            except ApiError as exc:
                self.log(f"close-env: {exc} (continuing; the environment may already be closed)")
            state = self._poll(lambda: c.get(f"/api/simulation/{simulation_id}/run-status"),
                         lambda s: s.get("runner_status") in ("completed", "stopped", "idle"),
                         "close-env", 600)
            return {"runner_status": state.get("runner_status"), "completed_at": state.get("completed_at")}

        self._stage("close_env", close_env)

        def report():
            data = c.post("/api/report/generate", {"simulation_id": simulation_id}, timeout=120)
            report_id = data.get("report_id")
            task_id = data.get("task_id")
            if report_id:
                self.manifest["report_id"] = report_id  # recorded now so purge finds a half-built report
                self._save()
            if not data.get("already_generated") and task_id:
                state = self._poll(lambda: c.post("/api/report/generate/status",
                                            {"task_id": task_id, "simulation_id": simulation_id}),
                             lambda s: s.get("status") == "completed", "report", stage_timeout_s)
                report_id = report_id or (state.get("result") or {}).get("report_id") or state.get("report_id")
            content = c.get(f"/api/report/{report_id}").get("markdown_content") or ""
            report_path = self.run_dir / "report.md"
            fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            return {"report_id": report_id, "report_file": report_path.name, "chars": len(content)}

        report_result = self._stage("report", report)
        self.manifest["report_id"] = report_result["report_id"]
        self._save()
        return self.manifest


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="default: MIROFISH_API_URL, else 127.0.0.1:<FLASK_PORT from .env>")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    run = sub.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--seed", action="append", required=True, type=Path)
    run.add_argument("--requirement-file", required=True, type=Path)
    run.add_argument("--max-rounds", type=int, default=10)
    run.add_argument("--platform", choices=PLATFORMS, default="parallel")
    run.add_argument("--max-agents", type=int)
    run.add_argument("--entity-type", action="append", dest="entity_types")
    run.add_argument("--graph-memory-update", action="store_true")
    run.add_argument("--until", choices=("prepare",))
    args = parser.parse_args(argv)

    client = MiroFishClient(args.base_url)
    if args.command == "health":
        ok = client.health()
        print("ok" if ok else "unreachable")
        return 0 if ok else 1
    requirement = args.requirement_file.read_text(encoding="utf-8").strip()
    manifest = RunPipeline(client, args.run_id, simulations_dir=SIMULATIONS_DIR).run(
        args.seed, requirement, max_rounds=args.max_rounds, platform=args.platform,
        max_agents=args.max_agents, entity_types=args.entity_types,
        enable_graph_memory_update=args.graph_memory_update, until=args.until)
    print(json.dumps({k: manifest.get(k) for k in ("run_id", "project_id", "graph_id", "simulation_id",
                                                   "report_id")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
