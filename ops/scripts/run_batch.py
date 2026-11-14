#!/usr/bin/env python3
"""Run a batch: a measured pilot first, then (after you confirm) the planned real runs.

  run_batch.py pilot  --batch BATCH_ID          # Q1, variant v0, rounds.pilot rounds; prints measurements, stops
  run_batch.py run    --batch BATCH_ID --confirm [--replan]
  run_batch.py status --batch BATCH_ID

Needs: prepare_inputs.py (and unknown_variants.py propose if the seed has UNKNOWNs), MiroFish
started with ops/scripts/start.sh, and LLM_BASE_URL routed through llm_meter.py, which is how
calls, time and structured-output failures are measured. Only one batch may run at a time
(calls are attributed to runs by time window).

Each run is a mirofish_api.RunPipeline under ops/runs/<RUN_ID>/ with a resumable manifest,
extended with: upstream commit and patch hashes, ops commit, model name and digests, limits,
seed/variant hashes, the variant mapping, per-stage timings and metered calls. Between prepare and
the rounds, the generated schedule is shifted so the simulated day starts at limits'
simulation.clock_start_hour (sim_schedule.py; upstream starts at 00:00). After the report,
the simulation's posts, comments and agent stances are exported to transcript.json for
factguard.py and compare.py. Re-running the same command resumes; finished runs are skipped.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plan as planner  # noqa: E402
from local_llm import LocalLLM  # noqa: E402
from mirofish_api import ApiError, MiroFishClient, RunPipeline, sha256_file, write_private_json  # noqa: E402
from opsconfig import (METER_LOG, OPS_DIR, RUNS_DIR, STATE_DIR, UPSTREAM_DIR, ensure_yaml, env_values,  # noqa: E402
                       git_info, make_private_dir, model_conf)
from prepare_inputs import batch_dir, load_inputs  # noqa: E402
from sim_schedule import apply_clock_shift, expected_turns  # noqa: E402
from unknown_variants import DEFAULT_RNG_SEED, sample  # noqa: E402

SIMULATIONS_DIR = UPSTREAM_DIR / "backend" / "uploads" / "simulations"
PLATFORM_COUNT = {"parallel": 2}
ALREADY_STARTED = ("starting", "running", "paused", "completed")
ENV_KEYS_RECORDED = ("LLM_MODEL_NAME", "LLM_BASE_URL", "MIROFISH_MEMORY_BACKEND", "LOCAL_GRAPH_EMBED_MODEL",
                     "LOCAL_GRAPH_EMBED_URL", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
ROUND_STAGE = "rounds"


class BatchError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- metering
def read_meter(path: Path = METER_LOG, start: float = 0.0, end: Optional[float] = None) -> List[Dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ts = row.get("ts", 0)
            if ts >= start and (end is None or ts <= end):
                rows.append(row)
    return rows


def summarize_calls(rows: Iterable[Dict]) -> Dict[str, Any]:
    rows = [r for r in rows if "completions" in (r.get("path") or "")]
    json_rows = [r for r in rows if r.get("json_mode") and r.get("status") == 200]
    tool_rows = [r for r in rows if r.get("tools") and r.get("status") == 200]
    with_calls = [r for r in tool_rows if r.get("tool_calls")]
    json_fail = sum(1 for r in json_rows if r.get("json_ok") is False)
    args_fail = sum(1 for r in with_calls if r.get("tool_args_ok") is False)
    structured = len(json_rows) + len(with_calls)
    latencies = sorted(r.get("latency_s", 0) for r in rows)
    return {
        "calls": len(rows),
        "http_errors": sum(1 for r in rows if r.get("status") != 200),
        "json_mode_calls": len(json_rows),
        "json_parse_failures": json_fail,
        "tool_calls_requested": len(tool_rows),
        "tool_calls_missing": len(tool_rows) - len(with_calls),
        "tool_argument_failures": args_fail,
        "structured_failure_rate": round((json_fail + args_fail) / structured, 4) if structured else None,
        "no_action_rate": round((len(tool_rows) - len(with_calls)) / len(tool_rows), 4) if tool_rows else None,
        "truncated": sum(1 for r in rows if r.get("finish_reason") == "length"),
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in rows),
        "completion_tokens": sum(r.get("completion_tokens") or 0 for r in rows),
        "median_latency_s": latencies[len(latencies) // 2] if latencies else None,
    }


def stage_metrics(manifest: Dict, meter_path: Path = METER_LOG) -> Dict[str, Dict]:
    """Calls and seconds per finished stage, using the stage's time window in the manifest."""
    metrics = {}
    for name, stage in (manifest.get("stages") or {}).items():
        if not stage.get("started_at"):
            continue
        end = stage.get("ended_at") or time.time()
        metrics[name] = {"seconds": round(end - stage["started_at"], 1),
                         **summarize_calls(read_meter(meter_path, stage["started_at"], end))}
    return metrics


def run_totals(per_stage: Dict[str, Dict], manifest: Dict, meter_path: Path = METER_LOG) -> Dict[str, Any]:
    stages = [s for s in (manifest.get("stages") or {}).values() if s.get("started_at")]
    if not stages:
        return {"calls": 0, "seconds": 0}
    start = min(s["started_at"] for s in stages)
    end = max(s.get("ended_at") or time.time() for s in stages)
    return {"seconds": round(sum(m["seconds"] for m in per_stage.values()), 1),
            "wall_seconds": round(end - start, 1), **summarize_calls(read_meter(meter_path, start, end))}


def cost_model_from_run(per_stage: Dict[str, Dict], rounds: int, schedule: Optional[Dict] = None,
                        estimates: Optional[Dict[str, float]] = None, platforms: int = 2) -> Dict[str, Any]:
    """Split the pilot's cost into fixed + per round + per expected agent turn.

    With a schedule, the rounds stage is charged per expected agent turn (on top of idle seconds per
    round), so plans for longer runs follow the agents' active hours instead of scaling linearly
    from a pilot that may have covered quiet hours. Without one (or with no expected turns), the
    rounds stage is linear per round and turns are costed from the estimates block.
    """
    if ROUND_STAGE not in per_stage or rounds < 1:
        raise BatchError("the pilot has no measured rounds stage")
    fixed = {k: v for k, v in per_stage.items() if k != ROUND_STAGE}
    measured = per_stage[ROUND_STAGE]
    model: Dict[str, Any] = {
        "fixed_calls": sum(v["calls"] for v in fixed.values()),
        "calls_per_round": round(measured["calls"] / rounds, 2),
        "fixed_seconds": round(sum(v["seconds"] for v in fixed.values()), 1),
        "seconds_per_round": round(measured["seconds"] / rounds, 2),
    }
    if schedule is None:
        return model
    est = estimates or {}
    turns = expected_turns(schedule, rounds, platforms)
    model.update(schedule=schedule, platforms=platforms, pilot_expected_turns=turns)
    if turns >= 1:
        idle = float(est.get("seconds_per_idle_round", 0.0))
        model.update(calls_per_round=0.0, seconds_per_round=idle,
                     calls_per_turn=round(measured["calls"] / turns, 3),
                     seconds_per_turn=round(max(0.0, measured["seconds"] - idle * rounds) / turns, 2))
    else:
        model.update(calls_per_turn=float(est.get("calls_per_agent_turn", 0.0)),
                     seconds_per_turn=float(est.get("seconds_per_agent_turn", 0.0)))
    return model


# ----------------------------------------------------------------------------- provenance
def provenance() -> Dict[str, Any]:
    conf = model_conf()
    env = env_values()
    patches = sorted((OPS_DIR / "patches").glob("*.patch"))
    llm = LocalLLM()
    embed = LocalLLM(model=conf.get("EMBED_MODEL_ALIAS", "mirofish-embed"))
    return {
        "upstream": git_info(UPSTREAM_DIR),
        "patches": [{"name": p.name, "sha256": sha256_file(p)} for p in patches],
        "ops": git_info(OPS_DIR.parent),
        "model": llm.model_info(),
        "embed_model": {k: v for k, v in embed.model_info().items() if k != "parameters"},
        "hf_model_ref": conf.get("HF_MODEL_REF"),
        "env": {k: env.get(k) for k in ENV_KEYS_RECORDED},
    }


# ----------------------------------------------------------------------------- transcript
def export_transcript(simulation_id: str, out_path: Path, simulations_dir: Path = SIMULATIONS_DIR) -> Dict:
    """Copy posts, comments, per-agent action counts and agent stances out of the simulation dir."""
    sim_dir = simulations_dir / simulation_id
    if not sim_dir.is_dir():
        raise BatchError(f"simulation directory {sim_dir} not found")
    config_path = sim_dir / "simulation_config.json"
    agents = []
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        keep = ("agent_id", "entity_name", "entity_type", "stance", "sentiment_bias", "activity_level", "influence_weight")
        agents = [{k: a.get(k) for k in keep} for a in config.get("agent_configs", [])]
    transcript: Dict[str, Any] = {"simulation_id": simulation_id, "agents": agents, "platforms": {}}
    for platform in ("reddit", "twitter"):
        db = sim_dir / f"{platform}_simulation.db"
        if not db.exists():
            continue
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            users = {r["user_id"]: dict(r) for r in connection.execute(
                "SELECT user_id, agent_id, user_name, name FROM user")}
            posts = [dict(r) for r in connection.execute(
                "SELECT post_id, user_id, original_post_id, content, quote_content, created_at, num_likes, "
                "num_dislikes, num_shares FROM post ORDER BY post_id")]
            comments = [dict(r) for r in connection.execute(
                "SELECT comment_id, post_id, user_id, content, created_at, num_likes, num_dislikes "
                "FROM comment ORDER BY comment_id")]
            actions = [dict(r) for r in connection.execute(
                "SELECT user_id, action, COUNT(*) AS n FROM trace GROUP BY user_id, action")]
        finally:
            connection.close()
        transcript["platforms"][platform] = {"users": list(users.values()), "posts": posts,
                                             "comments": comments, "actions": actions}
    write_private_json(out_path, transcript)
    return {"posts": sum(len(p["posts"]) for p in transcript["platforms"].values()),
            "comments": sum(len(p["comments"]) for p in transcript["platforms"].values()),
            "agents": len(agents)}


# ----------------------------------------------------------------------------- batch
class Batch:
    def __init__(self, batch_id: str, client: Optional[MiroFishClient] = None, log: Callable[[str], None] = print,
                 limits_path: Path = planner.DEFAULT_LIMITS, meter_path: Path = METER_LOG,
                 runs_dir: Path = RUNS_DIR, poll_interval_s: float = 5.0, check_meter: bool = True,
                 provenance_fn: Callable[[], Dict] = provenance, simulations_dir: Path = SIMULATIONS_DIR):
        self.batch_id = batch_id
        self.dir = batch_dir(batch_id)
        self.inputs = load_inputs(batch_id)
        self.limits_path = limits_path
        self.limits = planner.load_limits(limits_path)
        self.client = client or MiroFishClient()
        self.log = log
        self.meter_path = meter_path
        self.runs_dir = runs_dir
        self.poll_interval_s = poll_interval_s
        self.check_meter = check_meter
        self.provenance_fn = provenance_fn
        self.simulations_dir = simulations_dir
        self.state_path = self.dir / "batch.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() \
            else {"batch_id": batch_id, "runs": {}}

    # ----- preconditions
    def preflight(self) -> None:
        if not self.client.health():
            raise BatchError("MiroFish is not answering; start it with ops/scripts/start.sh")
        backend = env_values().get("MIROFISH_MEMORY_BACKEND", "")
        if backend != "local" and not self.inputs.get("pseudonymized"):
            raise BatchError(f"MIROFISH_MEMORY_BACKEND is '{backend}', not 'local', so the seed would go to Zep "
                             "Cloud; only a pseudonymised batch (prepare_inputs.py --terms) may run that way")
        if not self.check_meter:
            return
        port = model_conf().get("LLM_METER_PORT", "11435")
        llm_url = env_values().get("LLM_BASE_URL", "")
        if f"127.0.0.1:{port}" not in llm_url:
            raise BatchError(f"LLM_BASE_URL ({llm_url}) does not go through llm_meter.py on 127.0.0.1:{port}; "
                             "set it in mirofish/.env and restart with start.sh, or calls cannot be measured")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://127.0.0.1:{port}/meter/health", timeout=5) as response:
                if response.status != 200:
                    raise OSError(response.status)
        except OSError as exc:
            raise BatchError(f"llm_meter.py is not answering on port {port} ({exc}); restart with start.sh") from None

    def variants(self) -> Dict:
        """Sample enough variants for the largest possible plan (deterministic, cheap to redo)."""
        n = max(self.limits["runs"]["per_question"], self.limits["runs"]["per_question_high_unknown"])
        path = self.dir / "variants.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            alt = self.dir / "alternatives.json"
            current = hashlib.sha256(alt.read_bytes()).hexdigest() if alt.exists() else None
            if existing.get("n", 0) >= n and existing.get("alternatives_sha256") == current:
                return existing
            if self.state["runs"]:
                raise BatchError("alternatives.json changed after runs started; start a new batch")
        return sample(self.batch_id, n, DEFAULT_RNG_SEED)

    def _save_state(self) -> None:
        write_private_json(self.state_path, self.state)

    def requirement(self, question_id: str) -> str:
        question = next(q for q in self.inputs["questions"] if q["id"] == question_id)
        return (self.dir / question["requirement_file"]).read_text(encoding="utf-8")

    # ----- one run
    def shift_clock(self, simulation_id: str, rounds: int) -> Dict:
        """The 'schedule' stage: move the prepared simulation's clock to limits' clock_start_hour."""
        start_hour = self.limits["simulation"]["clock_start_hour"]
        status = self.client.get(f"/api/simulation/{simulation_id}/run-status").get("runner_status")
        if status in ALREADY_STARTED:
            self.log(f"schedule: simulation already {status}; its clock can no longer be shifted")
            return {"clock_start_hour": None, "changed": False, "skipped": f"simulation already {status}"}
        result = apply_clock_shift(simulation_id, start_hour, rounds,
                                   PLATFORM_COUNT.get(self.limits["platform"], 1), self.simulations_dir)
        self.log(f"schedule: clock starts at {start_hour:02d}:00 "
                 f"({'shifted' if result['changed'] else 'unchanged'}); "
                 f"expected agent turns in {rounds} rounds: {result['expected_agent_turns']:.0f}")
        return result

    def execute(self, run_id: str, question_id: str, variant: Dict, rounds: int, kind: str) -> Dict:
        seed_file = self.dir / variant["seed_file"]
        record = self.state["runs"].setdefault(run_id, {"question": question_id, "variant": variant["index"],
                                                        "rounds": rounds, "kind": kind})
        if record.get("status") == "done":
            self.log(f"{run_id}: already done, skipping")
            return record
        pipeline = RunPipeline(self.client, run_id, runs_dir=self.runs_dir, log=self.log,
                               poll_interval_s=self.poll_interval_s)
        if "batch" not in pipeline.manifest:
            pipeline.manifest["batch"] = {
                "batch_id": self.batch_id, "kind": kind, "question": question_id,
                "variant": {"index": variant["index"], "choices": variant["choices"]},
                "variants_rng_seed": json.loads((self.dir / "variants.json").read_text())["rng_seed"],
                "alternatives_sha256": variant.get("alternatives_sha256"),
                "seed": {"source_sha256": self.inputs["sources"]["seed"]["sha256"],
                         "upload_sha256": self.inputs["upload_seed_sha256"],
                         "variant_seed_sha256": variant["seed_sha256"],
                         "pseudonymized": self.inputs["pseudonymized"]},
                "limits": self.limits, "limits_sha256": sha256_file(self.limits_path),
                "provenance": self.provenance_fn(),
            }
        record.update(status="running", started_at=record.get("started_at") or time.time())
        self._save_state()
        try:
            manifest = pipeline.run([seed_file], self.requirement(question_id), max_rounds=rounds,
                                    platform=self.limits["platform"], max_agents=self.limits["caps"]["max_agents"],
                                    project_name=run_id,
                                    before_rounds=lambda simulation_id: self.shift_clock(simulation_id, rounds))
        except BaseException as exc:
            record.update(status="failed", error=str(exc)[:500])
            self._save_state()
            raise
        per_stage = stage_metrics(manifest, self.meter_path)
        totals = run_totals(per_stage, manifest, self.meter_path)
        exported = export_transcript(manifest["simulation_id"], pipeline.run_dir / "transcript.json",
                                     self.simulations_dir)
        agents = (manifest["stages"]["prepare"]["result"] or {}).get("expected_entities_count")
        rounds_result = manifest["stages"][ROUND_STAGE]["result"] or {}
        pipeline.manifest["batch"]["metrics"] = {"stages": per_stage, "total": totals, "agents": agents,
                                                 "rounds_completed": rounds_result.get("current_round"),
                                                 "transcript": exported}
        pipeline._save()
        record.update(status="done", ended_at=time.time(), calls=totals["calls"], seconds=totals["seconds"],
                      agents=agents, structured_failure_rate=totals["structured_failure_rate"])
        self._save_state()
        return record

    # ----- pilot
    def pilot(self) -> Dict:
        self.preflight()
        variants = self.variants()
        rounds = self.limits["rounds"]["pilot"]
        first = self.inputs["questions"][0]["id"]
        variant = dict(variants["variants"][0], alternatives_sha256=variants["alternatives_sha256"])
        run_id = f"{self.batch_id}_pilot"
        self.execute(run_id, first, variant, rounds, kind="pilot")
        manifest = json.loads((self.runs_dir / run_id / "manifest.json").read_text(encoding="utf-8"))
        metrics = manifest["batch"]["metrics"]
        measured_rounds = metrics.get("rounds_completed") or rounds
        schedule = ((manifest["stages"].get("schedule") or {}).get("result") or {}).get("schedule")
        pilot = {
            "run_id": run_id, "question": first, "rounds": measured_rounds, "agents": metrics["agents"],
            "total": metrics["total"], "stages": metrics["stages"],
            "cost_model": cost_model_from_run(metrics["stages"], measured_rounds, schedule, self.limits["estimates"],
                                              PLATFORM_COUNT.get(self.limits["platform"], 1)),
            "measured_at": time.time(),
        }
        write_private_json(self.dir / "pilot.json", pilot)
        return pilot

    # ----- real runs
    def confirmed_plan(self, replan: bool) -> Dict:
        path = self.dir / "plan.json"
        if path.exists() and not replan:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("confirmed_at"):
                return existing
        pilot_path = self.dir / "pilot.json"
        if not pilot_path.exists():
            raise BatchError(f"no pilot for {self.batch_id}; run: run_batch.py pilot --batch {self.batch_id}")
        context = planner.load_batch_context(self.dir)
        plan = planner.make_plan(self.limits, batch_id=self.batch_id, **context)
        if not plan["feasible"]:
            raise BatchError(planner.format_plan(plan))
        plan["confirmed_at"] = time.time()
        write_private_json(path, plan)
        return plan

    def run(self, replan: bool = False) -> List[Dict]:
        self.preflight()
        plan = self.confirmed_plan(replan)
        self.log(planner.format_plan(plan))
        variants = self.variants()
        caps = self.limits["caps"]
        per_run = planner.run_cost(plan["cost_model"], plan["rounds_per_run"])
        per_run_calls, per_run_seconds = per_run["calls"], per_run["seconds"]
        results = []
        for spec in plan["runs"]:
            done = [r for r in self.state["runs"].values() if r.get("kind") == "real" and r.get("status") == "done"]
            used_calls = sum(r.get("calls") or 0 for r in done)
            used_seconds = sum(r.get("seconds") or 0 for r in done)
            if self.state["runs"].get(spec["run_id"], {}).get("status") != "done":
                if used_calls + per_run_calls > caps["max_llm_calls"] or \
                        (used_seconds + per_run_seconds) / 60 > caps["wall_clock_minutes"]:
                    skipped = [s["run_id"] for s in plan["runs"]
                               if self.state["runs"].get(s["run_id"], {}).get("status") != "done"]
                    self.log(f"CAP: {used_calls} calls / {used_seconds / 60:.0f} min used; the next run would "
                             f"exceed the caps. Not started: {', '.join(skipped)}")
                    break
            variant = dict(variants["variants"][spec["variant"]], alternatives_sha256=variants["alternatives_sha256"])
            self.log(f"=== {spec['run_id']}: {spec['question']}, variant v{spec['variant']}, {spec['rounds']} rounds")
            results.append(self.execute(spec["run_id"], spec["question"], variant, spec["rounds"], kind="real"))
        return results


def format_pilot(pilot: Dict, plan: Dict) -> str:
    total, model = pilot["total"], pilot["cost_model"]
    rate = total["structured_failure_rate"]
    rounds_stage = pilot["stages"].get(ROUND_STAGE, {})
    if model.get("pilot_expected_turns", 0) >= 1:
        cost_line = (f"  LLM calls: {total['calls']} (fixed {model['fixed_calls']:.0f} + rounds {rounds_stage.get('calls')} "
                     f"for {model['pilot_expected_turns']:.0f} expected agent turns = "
                     f"{model['calls_per_turn']:.2f} per turn)")
        time_line = (f"  time: {total['wall_seconds'] / 60:.1f} min wall clock (fixed {model['fixed_seconds'] / 60:.1f} min "
                     f"+ {model['seconds_per_round']:.0f} s per round + {model['seconds_per_turn']:.1f} s per agent turn)")
    else:
        cost_line = f"  LLM calls: {total['calls']} (fixed {model['fixed_calls']:.0f} + {model['calls_per_round']} per round)"
        time_line = (f"  time: {total['wall_seconds'] / 60:.1f} min wall clock "
                     f"(fixed {model['fixed_seconds'] / 60:.1f} min + {model['seconds_per_round']} s per round)")
    lines = [
        f"PILOT {pilot['run_id']}: {pilot['rounds']} rounds, {pilot['agents']} agents",
        f"{cost_line}; HTTP errors {total['http_errors']}; truncated answers {total['truncated']}",
        time_line,
        f"  structured-output failure rate: {'n/a' if rate is None else f'{rate:.1%}'} "
        f"({total['json_parse_failures']} of {total['json_mode_calls']} JSON-mode answers unparseable, "
        f"{total['tool_argument_failures']} malformed tool calls); agents took no action in "
        f"{total['tool_calls_missing']} of {total['tool_calls_requested']} turns",
        "  per stage: " + ", ".join(f"{name} {m['calls']} calls/{m['seconds']:.0f}s" for name, m in pilot["stages"].items()),
        "",
        planner.format_plan(plan),
    ]
    return "\n".join(lines)


def format_status(batch: Batch) -> str:
    lines = [f"Batch {batch.batch_id}"]
    for run_id, record in sorted(batch.state["runs"].items()):
        lines.append(f"  {run_id:40} {record.get('kind', ''):6} {record.get('status', ''):8} "
                     f"{record.get('question')} v{record.get('variant')} {record.get('rounds')} rounds "
                     f"{record.get('calls', '-')} calls {record.get('seconds', '-')} s")
    return "\n".join(lines)


class BatchLock:
    def __init__(self, path: Path = STATE_DIR / "run_batch.lock"):
        self.path = path

    def __enter__(self):
        make_private_dir(self.path.parent)
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BatchError("another run_batch.py is running; batches must not overlap") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("pilot", "run", "status"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--confirm", action="store_true", help="required for 'run': you reviewed the pilot")
    parser.add_argument("--replan", action="store_true", help="re-plan from the pilot instead of the saved plan")
    parser.add_argument("--limits", type=Path, default=planner.DEFAULT_LIMITS)
    args = parser.parse_args(argv)

    try:
        batch = Batch(args.batch, limits_path=args.limits)
        if args.command == "status":
            print(format_status(batch))
            return 0
        with BatchLock():
            if args.command == "pilot":
                pilot = batch.pilot()
                context = planner.load_batch_context(batch.dir)
                plan = planner.make_plan(batch.limits, batch_id=args.batch, **context)
                print(format_pilot(pilot, plan))
                print(f"\nSTOP. Review the pilot (ops/runs/{pilot['run_id']}/report.md, transcript.json), then run:\n"
                      f"  ops/scripts/run_batch.py run --batch {args.batch} --confirm")
                return 0
            if not args.confirm:
                parser.error("'run' needs --confirm after you have reviewed the pilot")
            batch.run(replan=args.replan)
            print(format_status(batch))
            print(f"\nnext: ops/scripts/factguard.py --batch {args.batch}  and  ops/scripts/compare.py --batch {args.batch}")
            return 0
    except (BatchError, ApiError, FileNotFoundError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    ensure_yaml()
    sys.exit(main())
