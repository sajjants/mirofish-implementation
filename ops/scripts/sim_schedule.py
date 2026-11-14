#!/usr/bin/env python3
"""Agent schedules of a prepared MiroFish simulation: clock shift and expected agent turns.

Upstream starts every simulation clock at 00:00 (round r is hour (r * minutes_per_round // 60) % 24)
and only lets an agent act when that hour is in its active_hours, so the first rounds of every run
are night with nobody scheduled. The hour is used only to pick active agents and to label logs;
agents never see it. Shifting every hour list in the generated simulation_config.json by
clock_start_hour is therefore the same as starting the clock at that hour, without changing
MiroFish code. The shift is applied once, after prepare and before the rounds start, and marked
in the config (ops_clock_start_hour) so a resumed run does not shift twice.

expected_turns() is the exact expectation of upstream's get_active_agents_for_round()
(run_parallel_simulation.py @ the pinned commit): per round, T = int(Uniform(min, max) * m)
agents are drawn from the candidates C, each eligible agent being a candidate with probability
activity_level, so the round has min(T, C) agent turns per platform.

  sim_schedule.py --simulation SIM_ID [--rounds 10,24,30,40]   # print the schedule and expected turns
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opsconfig import UPSTREAM_DIR  # noqa: E402

SIMULATIONS_DIR = UPSTREAM_DIR / "backend" / "uploads" / "simulations"
CLOCK_MARKER = "ops_clock_start_hour"
SIMULATION_ID = re.compile(r"^sim_[A-Za-z0-9]{1,64}$")

# Upstream's fallbacks when a key is missing from the generated config.
DEFAULT_ACTIVE_HOURS = list(range(8, 23))
DEFAULT_ACTIVITY_LEVEL = 0.5
DEFAULTS = {
    "minutes_per_round": 30,
    "agents_per_hour_min": 5,
    "agents_per_hour_max": 20,
    "peak_hours": [9, 10, 11, 14, 15, 20, 21, 22],
    "peak_activity_multiplier": 1.5,
    "off_peak_hours": [0, 1, 2, 3, 4, 5],
    "off_peak_activity_multiplier": 0.3,
}
# Hour lists upstream reads for agent selection (materialised from DEFAULTS if absent) and
# descriptive ones it does not read (shifted only if present, to stay consistent).
SELECTION_HOUR_LISTS = ("peak_hours", "off_peak_hours")
OTHER_HOUR_LISTS = ("morning_hours", "work_hours")


def validate_start_hour(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 23:
        raise ValueError(f"clock_start_hour must be an integer 0-23 (got {value!r})")
    return value


def shift_hours(hours: Iterable[Any], start_hour: int) -> List[int]:
    return sorted({(int(h) - start_hour) % 24 for h in hours})


def shift_config(config: Dict[str, Any], start_hour: int) -> Dict[str, Any]:
    """Return a copy of a simulation config whose clock hour 0 behaves like start_hour."""
    start_hour = validate_start_hour(start_hour)
    marker = config.get(CLOCK_MARKER)
    if marker is not None:
        if marker != start_hour:
            raise ValueError(f"config already shifted to start at {marker}:00, not {start_hour}:00")
        return copy.deepcopy(config)
    shifted = copy.deepcopy(config)
    if start_hour == 0:
        return shifted
    time_config = shifted.setdefault("time_config", {})
    for key in SELECTION_HOUR_LISTS:
        time_config[key] = shift_hours(time_config.get(key, DEFAULTS[key]), start_hour)
    for key in OTHER_HOUR_LISTS:
        if key in time_config:
            time_config[key] = shift_hours(time_config[key], start_hour)
    for agent in shifted.get("agent_configs", []):
        agent["active_hours"] = shift_hours(agent.get("active_hours", DEFAULT_ACTIVE_HOURS), start_hour)
    shifted[CLOCK_MARKER] = start_hour
    return shifted


def schedule_of(config: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a simulation config that decide who acts when (no names, no content)."""
    time_config = config.get("time_config", {})
    schedule = {key: time_config.get(key, default) for key, default in DEFAULTS.items()}
    schedule["clock_start_hour"] = config.get(CLOCK_MARKER, 0)
    schedule["agents"] = [{"active_hours": list(a.get("active_hours", DEFAULT_ACTIVE_HOURS)),
                           "activity_level": float(a.get("activity_level", DEFAULT_ACTIVITY_LEVEL))}
                          for a in config.get("agent_configs", [])]
    return schedule


def _multiplier(schedule: Dict[str, Any], hour: int) -> float:
    if hour in schedule["peak_hours"]:
        return float(schedule["peak_activity_multiplier"])
    if hour in schedule["off_peak_hours"]:
        return float(schedule["off_peak_activity_multiplier"])
    return 1.0


def _prob_target_at_least(k: int, low: float, high: float, multiplier: float) -> float:
    """P(int(U(low, high) * multiplier) >= k) for k >= 1."""
    if multiplier <= 0:
        return 0.0
    threshold = k / multiplier
    if high <= low:
        return 1.0 if low >= threshold else 0.0
    return min(1.0, max(0.0, (high - threshold) / (high - low)))


def _candidate_tail(probabilities: List[float]) -> List[float]:
    """tail[k] = P(C >= k) for a sum of independent Bernoulli(p) (Poisson binomial)."""
    dist = [1.0]
    for p in probabilities:
        p = min(1.0, max(0.0, p))
        nxt = [0.0] * (len(dist) + 1)
        for count, mass in enumerate(dist):
            nxt[count] += mass * (1 - p)
            nxt[count + 1] += mass * p
        dist = nxt
    tail, running = [0.0] * (len(dist) + 1), 0.0
    for count in range(len(dist) - 1, -1, -1):
        running += dist[count]
        tail[count] = running
    return tail


def expected_turns_per_round(schedule: Dict[str, Any], rounds: int) -> List[float]:
    """Expected agent turns per round, per platform, for rounds 1..rounds."""
    minutes = int(schedule["minutes_per_round"]) or 60
    low, high = float(schedule["agents_per_hour_min"]), float(schedule["agents_per_hour_max"])
    result = []
    for index in range(rounds):
        hour = (index * minutes // 60) % 24
        eligible = [a["activity_level"] for a in schedule["agents"] if hour in a["active_hours"]]
        tail = _candidate_tail(eligible)
        multiplier = _multiplier(schedule, hour)
        result.append(sum(_prob_target_at_least(k, low, high, multiplier) * tail[k]
                          for k in range(1, len(eligible) + 1)))
    return result


def expected_turns(schedule: Dict[str, Any], rounds: int, platforms: int = 2) -> float:
    return round(platforms * sum(expected_turns_per_round(schedule, rounds)), 2)


def simulation_dir(simulation_id: str, simulations_dir: Path = SIMULATIONS_DIR) -> Path:
    if not SIMULATION_ID.match(simulation_id or ""):
        raise ValueError(f"unexpected simulation id {simulation_id!r}")
    return simulations_dir / simulation_id


def _write_like(path: Path, text: str) -> None:
    """Atomically replace path, keeping its permission bits."""
    mode = path.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(prefix=".ops-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def apply_clock_shift(simulation_id: str, start_hour: int, rounds: int, platforms: int = 2,
                      simulations_dir: Path = SIMULATIONS_DIR) -> Dict[str, Any]:
    """Shift a prepared simulation's schedule in place; return what was done and the expected turns."""
    path = simulation_dir(simulation_id, simulations_dir) / "simulation_config.json"
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8"))
    shifted = shift_config(config, start_hour)
    changed = shifted != config
    if changed:
        _write_like(path, json.dumps(shifted, ensure_ascii=False, indent=2))
    schedule = schedule_of(shifted)
    return {
        "clock_start_hour": start_hour,
        "changed": changed,
        "config_sha256_before": hashlib.sha256(raw).hexdigest(),
        "config_sha256_after": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schedule": schedule,
        "expected_agent_turns": expected_turns(schedule, rounds, platforms),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulation", required=True)
    parser.add_argument("--rounds", default="10,24,30,40")
    args = parser.parse_args(argv)
    path = simulation_dir(args.simulation) / "simulation_config.json"
    schedule = schedule_of(json.loads(path.read_text(encoding="utf-8")))
    print(f"{args.simulation}: clock starts at {schedule['clock_start_hour']:02d}:00, "
          f"{schedule['minutes_per_round']} min per round, {len(schedule['agents'])} agents, "
          f"{schedule['agents_per_hour_min']}-{schedule['agents_per_hour_max']} active per hour")
    for rounds in (int(x) for x in args.rounds.split(",")):
        print(f"  {rounds:3} rounds: {expected_turns(schedule, rounds):6.1f} expected agent turns (both platforms)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
