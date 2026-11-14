#!/usr/bin/env python3
"""Deterministic run planner: how many runs, how many rounds, within which caps.

  plan.py --batch BATCH_ID [--write]                  # uses the batch inputs and pilot.json if present
  plan.py --questions 3 --unknowns 2 [--agents 25]    # ad hoc
  options: --limits ops/config/limits.yaml

Rules (ops/config/limits.yaml): real runs use rounds.real_max rounds; each question gets
runs.per_question runs, or runs.per_question_high_unknown when the seed has more than
runs.unknown_threshold UNKNOWN facts. If the estimate exceeds caps.max_llm_calls or
caps.wall_clock_minutes, rounds are cut first (down to rounds.real_min), then runs; every cut
is printed. Cost per run = fixed + per_round * rounds, from the pilot if measured, else from
the estimates block.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mirofish_api import PLATFORMS  # noqa: E402
from opsconfig import OPS_DIR, RUNS_DIR, ensure_yaml, load_yaml  # noqa: E402

DEFAULT_LIMITS = OPS_DIR / "config" / "limits.yaml"


def _positive_int(section: Dict, key: str, where: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"limits: {where}.{key} must be a positive integer (got {value!r})")
    return value


def _positive_number(section: Dict, key: str, where: str) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"limits: {where}.{key} must be a non-negative number (got {value!r})")
    return float(value)


def validate_limits(raw: Dict[str, Any]) -> Dict[str, Any]:
    rounds, runs, caps, est = (raw.get(k) or {} for k in ("rounds", "runs", "caps", "estimates"))
    limits = {
        "rounds": {k: _positive_int(rounds, k, "rounds") for k in ("pilot", "real_min", "real_max", "hard_ceiling")},
        "runs": {k: _positive_int(runs, k, "runs") for k in ("per_question", "per_question_high_unknown")},
        "caps": {k: _positive_int(caps, k, "caps") for k in ("max_llm_calls", "wall_clock_minutes", "max_agents")},
        "estimates": {k: _positive_number(est, k, "estimates")
                      for k in ("fixed_calls_per_run", "calls_per_round", "fixed_seconds_per_run", "seconds_per_round")},
        "platform": raw.get("platform", "parallel"),
    }
    threshold = runs.get("unknown_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
        raise ValueError(f"limits: runs.unknown_threshold must be an integer >= 0 (got {threshold!r})")
    limits["runs"]["unknown_threshold"] = threshold
    r = limits["rounds"]
    if r["hard_ceiling"] > 40:
        raise ValueError("limits: rounds.hard_ceiling cannot exceed 40")
    if not r["real_min"] <= r["real_max"] <= r["hard_ceiling"] or r["pilot"] > r["hard_ceiling"]:
        raise ValueError("limits: need real_min <= real_max <= hard_ceiling and pilot <= hard_ceiling")
    if limits["platform"] not in PLATFORMS:
        raise ValueError(f"limits: platform must be one of {PLATFORMS} (upstream's single-platform scripts "
                         "never report the end of the rounds)")
    return limits


def load_limits(path: Path = DEFAULT_LIMITS) -> Dict[str, Any]:
    return validate_limits(load_yaml(path))


def cost_model(limits: Dict[str, Any], measured: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if measured:
        return {"source": "pilot", **{k: float(measured[k]) for k in
                                      ("fixed_calls", "calls_per_round", "fixed_seconds", "seconds_per_round")}}
    est = limits["estimates"]
    return {"source": "estimates", "fixed_calls": est["fixed_calls_per_run"], "calls_per_round": est["calls_per_round"],
            "fixed_seconds": est["fixed_seconds_per_run"], "seconds_per_round": est["seconds_per_round"]}


def make_plan(limits: Dict[str, Any], question_ids: List[str], unknown_count: int,
              agents: Optional[int] = None, measured: Optional[Dict[str, Any]] = None,
              batch_id: str = "batch") -> Dict[str, Any]:
    if not question_ids:
        raise ValueError("no questions to plan")
    model = cost_model(limits, measured)
    caps, r, runs_cfg = limits["caps"], limits["rounds"], limits["runs"]
    n_questions = len(question_ids)

    def totals(rounds: int, runs: int) -> Dict[str, float]:
        per_run_calls = model["fixed_calls"] + model["calls_per_round"] * rounds
        per_run_seconds = model["fixed_seconds"] + model["seconds_per_round"] * rounds
        return {"calls": round(n_questions * runs * per_run_calls),
                "minutes": round(n_questions * runs * per_run_seconds / 60, 1)}

    def fits(rounds: int, runs: int) -> bool:
        t = totals(rounds, runs)
        return t["calls"] <= caps["max_llm_calls"] and t["minutes"] <= caps["wall_clock_minutes"]

    def over(rounds: int, runs: int) -> str:
        t = totals(rounds, runs)
        parts = []
        if t["calls"] > caps["max_llm_calls"]:
            parts.append(f"{t['calls']} calls > cap {caps['max_llm_calls']}")
        if t["minutes"] > caps["wall_clock_minutes"]:
            parts.append(f"{t['minutes']} min > cap {caps['wall_clock_minutes']}")
        return ", ".join(parts)

    high_unknown = unknown_count > runs_cfg["unknown_threshold"]
    wanted_runs = runs_cfg["per_question_high_unknown"] if high_unknown else runs_cfg["per_question"]
    wanted_rounds = min(r["real_max"], r["hard_ceiling"])
    rounds, runs, cuts = wanted_rounds, wanted_runs, []

    if not fits(rounds, runs):
        reason = over(rounds, runs)
        while rounds > r["real_min"] and not fits(rounds, runs):
            rounds -= 1
        cuts.append(f"rounds per run {wanted_rounds} -> {rounds} ({reason})")
    while runs > 1 and not fits(rounds, runs):
        reason = over(rounds, runs)
        runs -= 1
        cuts.append(f"runs per question {runs + 1} -> {runs} ({reason})")
        best = max((x for x in range(r["real_min"], wanted_rounds + 1) if fits(x, runs)), default=rounds)
        if best != rounds:
            cuts.append(f"rounds per run {rounds} -> {best} (budget freed by the dropped run)")
            rounds = best
    feasible = fits(rounds, runs)

    warnings = []
    if model["source"] == "estimates":
        warnings.append("cost is estimated from limits.yaml; run the pilot to measure it on this machine")
    if agents is not None and agents > caps["max_agents"]:
        warnings.append(f"{agents} agents exceeds caps.max_agents={caps['max_agents']}: every typed graph entity "
                        "becomes an agent and upstream has no cap; trim the seed or pass entity types")
    if unknown_count and runs < 2:
        warnings.append("only one run per question: UNKNOWN variants cannot be compared")
    if not feasible:
        warnings.append(f"INFEASIBLE: even 1 run x {rounds} rounds per question exceeds the caps "
                        f"({over(rounds, runs)}); raise the caps or ask fewer questions")

    planned_runs = [{"run_id": f"{batch_id}_{q.lower()}_r{i + 1}", "question": q, "variant": i, "rounds": rounds}
                    for q in question_ids for i in range(runs)]
    return {
        "batch_id": batch_id,
        "feasible": feasible,
        "platform": limits["platform"],
        "questions": list(question_ids),
        "unknown_count": unknown_count,
        "high_unknown": high_unknown,
        "agents": agents,
        "runs_per_question": runs,
        "rounds_per_run": rounds,
        "variants_needed": runs,
        "estimate": totals(rounds, runs),
        "caps": dict(caps),
        "cost_model": model,
        "cuts": cuts,
        "warnings": warnings,
        "runs": planned_runs if feasible else [],
    }


def format_plan(plan: Dict[str, Any]) -> str:
    lines = [
        f"Plan for {plan['batch_id']}: {len(plan['questions'])} question(s) x {plan['runs_per_question']} run(s) "
        f"x {plan['rounds_per_run']} rounds on {plan['platform']}",
        f"  estimate: {plan['estimate']['calls']} LLM calls, {plan['estimate']['minutes']} min "
        f"(caps {plan['caps']['max_llm_calls']} calls, {plan['caps']['wall_clock_minutes']} min; "
        f"cost from {plan['cost_model']['source']})",
        f"  UNKNOWN facts: {plan['unknown_count']}; agents: {plan['agents'] if plan['agents'] is not None else 'not measured'}",
    ]
    lines += [f"  CUT: {c}" for c in plan["cuts"]]
    lines += [f"  WARNING: {w}" for w in plan["warnings"]]
    return "\n".join(lines)


def load_batch_context(batch_dir: Path) -> Dict[str, Any]:
    inputs = json.loads((batch_dir / "inputs.json").read_text(encoding="utf-8"))
    pilot_path = batch_dir / "pilot.json"
    pilot = json.loads(pilot_path.read_text(encoding="utf-8")) if pilot_path.exists() else None
    return {
        "question_ids": [q["id"] for q in inputs["questions"]],
        "unknown_count": len(inputs["unknowns"]),
        "agents": (pilot or {}).get("agents"),
        "measured": (pilot or {}).get("cost_model"),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limits", type=Path, default=DEFAULT_LIMITS)
    parser.add_argument("--batch")
    parser.add_argument("--write", action="store_true", help="save plan.json in the batch directory")
    parser.add_argument("--questions", type=int)
    parser.add_argument("--unknowns", type=int, default=0)
    parser.add_argument("--agents", type=int)
    args = parser.parse_args(argv)

    limits = load_limits(args.limits)
    if args.batch:
        from mirofish_api import validate_run_id, write_private_json
        batch_dir = RUNS_DIR / validate_run_id(args.batch)
        context = load_batch_context(batch_dir)
        if args.agents is not None:
            context["agents"] = args.agents
        plan = make_plan(limits, batch_id=args.batch, **context)
        if args.write:
            write_private_json(batch_dir / "plan.json", plan)
    elif args.questions:
        plan = make_plan(limits, [f"Q{i + 1}" for i in range(args.questions)], args.unknowns, args.agents)
    else:
        parser.error("give --batch or --questions")
    print(format_plan(plan))
    return 0 if plan["feasible"] else 1


if __name__ == "__main__":
    ensure_yaml()
    sys.exit(main())
