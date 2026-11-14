"""Tests for the Phase 4 wrapper: seed parsing and lint, pseudonymisation, planner, variants,
LLM meter, batch runner, factguard and compare. No real MiroFish or LLM needed.

Run with the backend interpreter (it has PyYAML from upstream's lockfile):
  mirofish/backend/.venv/bin/python -m unittest discover -s ops/tests -v
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

OPS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR / "scripts"))
sys.path.insert(0, str(OPS_DIR / "tests"))
import compare  # noqa: E402
import depseudonymize  # noqa: E402
import factguard  # noqa: E402
import llm_meter  # noqa: E402
import mirofish_api  # noqa: E402
import plan as planner  # noqa: E402
import prepare_inputs  # noqa: E402
import pseudonymize  # noqa: E402
import purge  # noqa: E402
import run_batch  # noqa: E402
import seedfile  # noqa: E402
import sim_schedule  # noqa: E402
import unknown_variants  # noqa: E402
from local_llm import LLMError  # noqa: E402
from test_phase3 import FakeMiroFish  # noqa: E402

FIXTURES = OPS_DIR / "tests" / "fixtures"
SEED = (FIXTURES / "synthetic_seed.md").read_text(encoding="utf-8")
QUESTIONS = "# Questions\n\n## Q1: Will Proposal 14 pass?\nConsider the vote.\n\n## Q2\nHow will the union react?\n"
CANARIES = ("ZEBRA-CANARY-7731", "CANARY-REPORT-4410", "CanaryEntityType9", "PURPLE-HERON-2208")

RAW_LIMITS = {
    "rounds": {"pilot": 10, "real_min": 20, "real_max": 30, "hard_ceiling": 40},
    "runs": {"per_question": 2, "per_question_high_unknown": 3, "unknown_threshold": 2},
    "caps": {"max_llm_calls": 6000, "wall_clock_minutes": 600, "max_agents": 30},
    "platform": "parallel",
    "simulation": {"clock_start_hour": 8},
    "estimates": {"fixed_calls_per_run": 120, "calls_per_round": 12, "fixed_seconds_per_run": 1200,
                  "seconds_per_round": 8, "seconds_per_idle_round": 3, "calls_per_agent_turn": 1.5,
                  "seconds_per_agent_turn": 12},
}


# Shaped like the Phase 4 pilot's generated config: day agents, evening agents, a night clock start.
SCHEDULE_CONFIG = {
    "time_config": {"total_simulation_hours": 72, "minutes_per_round": 60, "agents_per_hour_min": 3,
                    "agents_per_hour_max": 5, "peak_hours": [19, 20, 21, 22], "peak_activity_multiplier": 1.5,
                    "off_peak_hours": [0, 1, 2, 3, 4, 5], "off_peak_activity_multiplier": 0.05,
                    "work_hours": list(range(9, 19))},
    "agent_configs": [
        {"agent_id": 0, "active_hours": list(range(9, 17)), "activity_level": 0.4},
        {"agent_id": 1, "active_hours": list(range(18, 23)), "activity_level": 0.8},
        {"agent_id": 2, "active_hours": list(range(18, 23)), "activity_level": 0.6},
        {"agent_id": 3, "active_hours": list(range(8, 23)), "activity_level": 0.5},
        {"agent_id": 4},
    ],
}


def limits(**overrides):
    raw = copy.deepcopy(RAW_LIMITS)
    for dotted, value in overrides.items():
        section, key = dotted.split("__")
        raw[section][key] = value
    return planner.validate_limits(raw)


class FakeLLM:
    """Stands in for local_llm.LocalLLM: returns canned answers and applies the caller's validator."""

    model = "fake-llm"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def chat_json(self, system, user, *, validate, seed=0, temperature=0.2, retries=3, max_tokens=2048):
        last = None
        for _ in range(retries):
            self.prompts.append(user)
            answer = self.answers.pop(0) if len(self.answers) > 1 else copy.deepcopy(self.answers[0])
            answer = answer(user) if callable(answer) else copy.deepcopy(answer)
            last = validate(answer)
            if not last:
                return answer
        raise LLMError(last)

    def model_info(self):
        return {"name": self.model}


class SeedFileTests(unittest.TestCase):
    def test_parse_facts_assigns_ids(self):
        facts = seedfile.parse_facts("- VERIFIED: a\n* UNKNOWN: b\nUNKNOWN [U-x]: c\nESTIMATE: d\nnot a fact\n")
        self.assertEqual([(f.id, f.tag) for f in facts],
                         [("V1", "VERIFIED"), ("U1", "UNKNOWN"), ("U-x", "UNKNOWN"), ("E1", "ESTIMATE")])
        with self.assertRaises(ValueError):
            seedfile.parse_facts("UNKNOWN: a\nUNKNOWN [U1]: b\n")

    def test_parse_questions(self):
        questions = seedfile.parse_questions(QUESTIONS)
        self.assertEqual([q.id for q in questions], ["Q1", "Q2"])
        self.assertEqual(questions[0].text, "Will Proposal 14 pass?\n\nConsider the vote.")
        self.assertEqual(questions[1].text, "How will the union react?")
        with self.assertRaises(ValueError):
            seedfile.parse_questions("## Q1\nx\n## Q1\ny\n")
        with self.assertRaises(ValueError):
            seedfile.parse_questions("## Q1\n\n## Q2\ny\n")

    def test_lint_removes_injected_instructions_and_keeps_facts(self):
        clean, findings = seedfile.lint_seed(SEED)
        for canary in CANARIES:
            self.assertNotIn(canary, clean)
        self.assertEqual(len(findings), 4)
        self.assertEqual(len(seedfile.parse_facts(clean)), len(seedfile.parse_facts(SEED)))
        self.assertIn("worried about the noise", clean)
        kept, findings = seedfile.lint_seed(SEED, keep_flagged=True)
        self.assertIn("ZEBRA-CANARY-7731", kept)
        self.assertNotIn("PURPLE-HERON-2208", kept)  # HTML comments are always removed
        self.assertTrue(all(f["action"] == "kept" for f in findings if f["rule"] != "html-comment"))

    def test_apply_variant_replaces_unknown_lines(self):
        text = "# S\n- VERIFIED: a\n- UNKNOWN: will they strike?\n"
        facts = seedfile.parse_facts(text)
        result = seedfile.apply_variant(text, facts, {"U1": "They strike."})
        self.assertEqual(result, "# S\n- VERIFIED: a\n- SCENARIO ASSUMPTION [U1]: They strike.\n")
        with self.assertRaises(KeyError):
            seedfile.apply_variant(text, facts, {})


class PseudonymizeTests(unittest.TestCase):
    TERMS = "person: Elena Duarte\nperson: Tomas Reyes | Tomas\nplace: Riverton\norg: Greenway Coalition # env group\n"
    TEXT = ("Mayor Elena Duarte met Councillor Reyes in Riverton. Tomas left. The Riverton City Council and the "
            "Greenway Coalition disagree. Savings of 4.2 million dollars; 62 buses; 410 drivers; 1,800 signatures; "
            "$250,000 grant; 30 percent; 45%; in 2026; ticket CASE-7731; version 2.10.\n")

    def run_once(self, seed=7):
        return pseudonymize.pseudonymize_texts({"seed": self.TEXT}, pseudonymize.parse_terms(self.TERMS), seed)

    def test_names_replaced_consistently(self):
        out, mapping = self.run_once()
        text = out["seed"]
        for original in ("Elena", "Duarte", "Reyes", "Tomas", "Riverton", "Greenway"):
            self.assertNotIn(original, text)
        names = {e["original"]: e["pseudonym"] for e in mapping["names"]}
        self.assertTrue(names["Elena Duarte"].endswith(names["Duarte"]))
        self.assertIn(f"Councillor {names['Reyes']}", text)
        self.assertIn(f"{names['Riverton']} City Council", text)
        self.assertEqual(names["Tomas"], names["Tomas Reyes"].split()[0])

    def test_figures_scaled_but_years_percent_ids_kept(self):
        out, mapping = self.run_once()
        text = out["seed"]
        for kept in ("62 buses", "30 percent", "45%", "in 2026", "CASE-7731", "version 2.10"):
            self.assertIn(kept, text)
        for scaled in ("4.2 million dollars", "410 drivers", "1,800 signatures", "$250,000"):
            self.assertNotIn(scaled, text)
        figures = {e["original"]: e["pseudonym"] for e in mapping["figures"]}
        self.assertRegex(figures["1,800"], r"^\d{1,3},\d{3}$")
        self.assertTrue(0.7 <= mapping["scale_factor"] <= 1.4)

    def test_round_trip_and_determinism(self):
        out, mapping = self.run_once()
        restored, unmapped = depseudonymize.depseudonymize(out["seed"], mapping)
        self.assertEqual(restored, self.TEXT)
        self.assertEqual(unmapped, [])
        self.assertEqual(self.run_once()[0], out)
        _, other = self.run_once(seed=8)
        self.assertNotEqual(other["names"], mapping["names"])

    def test_derived_amounts_reported_as_unmapped(self):
        _, mapping = self.run_once()
        _, unmapped = depseudonymize.depseudonymize("The model said $9.9 million in total.", mapping)
        self.assertEqual(unmapped, ["$9.9 million"])

    def test_leak_detection(self):
        engine = pseudonymize.Pseudonymizer(1)
        engine.add_terms(pseudonymize.parse_terms("person: Elena Duarte"), "")
        self.assertEqual(engine.leaks("ELENA DUARTE spoke"), ["Elena Duarte", "Duarte"])
        self.assertEqual(engine.leaks("Duarte's office"), ["Duarte"])
        self.assertEqual(engine.leaks("Duartes and Elena"), [])

    def test_suggest_lists_uncovered_names(self):
        found = pseudonymize.suggest("Mayor Elena Duarte met the Harbor Street Merchants Association.",
                                     pseudonymize.parse_terms("person: Elena Duarte"))
        self.assertIn("Harbor Street Merchants Association", found)
        self.assertFalse(any("Duarte" in f for f in found))


class PlanTests(unittest.TestCase):
    def test_default_plan(self):
        plan = planner.make_plan(limits(), ["Q1", "Q2"], unknown_count=2, agents=12)
        self.assertEqual((plan["runs_per_question"], plan["rounds_per_run"]), (2, 30))
        self.assertEqual(plan["estimate"], {"calls": 1920, "minutes": 96.0})
        self.assertEqual([r["run_id"] for r in plan["runs"]], ["batch_q1_r1", "batch_q1_r2", "batch_q2_r1", "batch_q2_r2"])
        self.assertEqual([r["variant"] for r in plan["runs"]], [0, 1, 0, 1])
        self.assertEqual(plan["cuts"], [])
        self.assertEqual(plan, planner.make_plan(limits(), ["Q1", "Q2"], unknown_count=2, agents=12))

    def test_high_unknown_count_gives_three_runs(self):
        self.assertEqual(planner.make_plan(limits(), ["Q1"], unknown_count=3)["runs_per_question"], 3)

    def test_cuts_rounds_first(self):
        plan = planner.make_plan(limits(), [f"Q{i}" for i in range(5)], unknown_count=4)
        self.assertEqual((plan["runs_per_question"], plan["rounds_per_run"]), (3, 23))
        self.assertEqual(len(plan["cuts"]), 1)
        self.assertIn("rounds per run 30 -> 23", plan["cuts"][0])

    def test_then_cuts_runs_and_reuses_freed_budget(self):
        plan = planner.make_plan(limits(caps__max_llm_calls=3000), [f"Q{i}" for i in range(5)], unknown_count=4)
        self.assertTrue(plan["feasible"])
        self.assertEqual((plan["runs_per_question"], plan["rounds_per_run"]), (1, 30))
        self.assertEqual([c.split(" (")[0] for c in plan["cuts"]],
                         ["rounds per run 30 -> 20", "runs per question 3 -> 2", "runs per question 2 -> 1",
                          "rounds per run 20 -> 30"])
        self.assertTrue(any("cannot be compared" in w for w in plan["warnings"]))

    def test_small_and_large_inputs_respect_limits(self):
        lim = limits()
        small = planner.make_plan(lim, ["Q1"], unknown_count=0, agents=8)
        large = planner.make_plan(lim, [f"Q{i}" for i in range(8)], unknown_count=5, agents=40)
        self.assertNotEqual((small["runs_per_question"], small["rounds_per_run"]),
                            (large["runs_per_question"], large["rounds_per_run"]))
        for plan in (small, large):
            self.assertTrue(plan["feasible"])
            self.assertTrue(lim["rounds"]["real_min"] <= plan["rounds_per_run"] <= lim["rounds"]["hard_ceiling"])
            self.assertLessEqual(plan["estimate"]["calls"], lim["caps"]["max_llm_calls"])
            self.assertLessEqual(plan["estimate"]["minutes"], lim["caps"]["wall_clock_minutes"])
            self.assertTrue(all(r["rounds"] == plan["rounds_per_run"] for r in plan["runs"]))
        self.assertEqual((small["runs_per_question"], small["rounds_per_run"], small["cuts"]), (2, 30, []))
        self.assertEqual((large["runs_per_question"], large["rounds_per_run"]), (2, 21))
        kinds = [c.split(" (")[0] for c in large["cuts"]]
        self.assertEqual(kinds, ["rounds per run 30 -> 20", "runs per question 3 -> 2", "rounds per run 20 -> 21"])
        self.assertIn("budget freed", large["cuts"][-1])  # only freed budget may raise rounds after a run cut
        self.assertTrue(any("40 agents exceeds" in w for w in large["warnings"]))

    def test_infeasible_plan_has_no_runs(self):
        plan = planner.make_plan(limits(caps__max_llm_calls=100), ["Q1"], unknown_count=0)
        self.assertFalse(plan["feasible"])
        self.assertEqual(plan["runs"], [])
        self.assertTrue(any(w.startswith("INFEASIBLE") for w in plan["warnings"]))

    def test_agent_cap_warning_and_measured_costs(self):
        measured = {"fixed_calls": 50, "calls_per_round": 5, "fixed_seconds": 300, "seconds_per_round": 6}
        plan = planner.make_plan(limits(), ["Q1"], unknown_count=0, agents=31, measured=measured)
        self.assertEqual(plan["cost_model"]["source"], "pilot")
        self.assertEqual(plan["estimate"]["calls"], 2 * (50 + 5 * 30))
        self.assertTrue(any("31 agents exceeds" in w for w in plan["warnings"]))

    def test_limits_validation(self):
        for section, key, value in (("rounds", "real_min", 35), ("rounds", "hard_ceiling", 41),
                                    ("caps", "max_llm_calls", 0), ("runs", "per_question", True),
                                    ("platform", None, "mastodon"), ("platform", None, "reddit"),
                                    ("simulation", "clock_start_hour", 24), ("simulation", "clock_start_hour", "8")):
            raw = copy.deepcopy(RAW_LIMITS)
            if key is None:
                raw[section] = value
            else:
                raw[section][key] = value
            with self.assertRaises(ValueError, msg=f"{section}.{key}={value}"):
                planner.validate_limits(raw)

    def test_shipped_limits_file_is_valid(self):
        loaded = planner.load_limits()
        self.assertLessEqual(loaded["rounds"]["hard_ceiling"], 40)
        self.assertEqual(loaded["rounds"]["pilot"], 10)
        self.assertEqual(loaded["simulation"]["clock_start_hour"], 8)

    def test_measured_schedule_costs_per_agent_turn(self):
        schedule = sim_schedule.schedule_of(sim_schedule.shift_config(SCHEDULE_CONFIG, 8))
        measured = {"fixed_calls": 50, "calls_per_round": 0, "fixed_seconds": 300, "seconds_per_round": 3,
                    "calls_per_turn": 1.25, "seconds_per_turn": 10, "schedule": schedule, "platforms": 2}
        plan = planner.make_plan(limits(), ["Q1"], unknown_count=0, measured=measured)
        turns = sim_schedule.expected_turns(schedule, 30)
        self.assertEqual(plan["expected_agent_turns_per_run"], turns)
        self.assertEqual(plan["estimate"]["calls"], round(2 * (50 + 1.25 * turns)))
        self.assertEqual(plan["estimate"]["minutes"], round(2 * (300 + 3 * 30 + 10 * turns) / 60, 1))
        self.assertIn("clock starts at 08:00", planner.format_plan(plan))


def upstream_active_count(config, hour, rng):
    """Re-statement of upstream get_active_agents_for_round() (pinned commit), returning len(selected)."""
    tc = config.get("time_config", {})
    if hour in tc.get("peak_hours", [9, 10, 11, 14, 15, 20, 21, 22]):
        multiplier = tc.get("peak_activity_multiplier", 1.5)
    elif hour in tc.get("off_peak_hours", [0, 1, 2, 3, 4, 5]):
        multiplier = tc.get("off_peak_activity_multiplier", 0.3)
    else:
        multiplier = 1.0
    target = int(rng.uniform(tc.get("agents_per_hour_min", 5), tc.get("agents_per_hour_max", 20)) * multiplier)
    candidates = [a for a in config["agent_configs"]
                  if hour in a.get("active_hours", list(range(8, 23))) and rng.random() < a.get("activity_level", 0.5)]
    return min(target, len(candidates))


class ScheduleTests(unittest.TestCase):
    def test_shift_moves_every_hour_list_once(self):
        shifted = sim_schedule.shift_config(SCHEDULE_CONFIG, 8)
        tc = shifted["time_config"]
        self.assertEqual(tc["peak_hours"], [11, 12, 13, 14])
        self.assertEqual(tc["off_peak_hours"], [16, 17, 18, 19, 20, 21])
        self.assertEqual(tc["work_hours"], list(range(1, 11)))
        self.assertEqual(shifted["agent_configs"][0]["active_hours"], list(range(1, 9)))
        self.assertEqual(shifted["agent_configs"][4]["active_hours"], list(range(0, 15)))  # upstream default 8-22
        self.assertEqual(shifted[sim_schedule.CLOCK_MARKER], 8)
        self.assertEqual(SCHEDULE_CONFIG["agent_configs"][0]["active_hours"], list(range(9, 17)))  # input untouched
        self.assertEqual(sim_schedule.shift_config(shifted, 8), shifted)  # idempotent
        with self.assertRaises(ValueError):
            sim_schedule.shift_config(shifted, 6)
        self.assertEqual(sim_schedule.shift_config(SCHEDULE_CONFIG, 0), SCHEDULE_CONFIG)

    def test_expected_turns_match_upstream_selection(self):
        import random
        rng = random.Random(7)
        for start in (0, 8):
            config = sim_schedule.shift_config(SCHEDULE_CONFIG, start)
            schedule = sim_schedule.schedule_of(config)
            per_round = sim_schedule.expected_turns_per_round(schedule, 30)
            for index in (0, 3, 10, 14, 20):
                trials = 6000
                mean = sum(upstream_active_count(config, index % 24, rng) for _ in range(trials)) / trials
                self.assertAlmostEqual(per_round[index], mean, delta=0.08, msg=f"start {start} round {index + 1}")
        night = sim_schedule.expected_turns(sim_schedule.schedule_of(SCHEDULE_CONFIG), 10)
        day = sim_schedule.expected_turns(sim_schedule.schedule_of(sim_schedule.shift_config(SCHEDULE_CONFIG, 8)), 10)
        self.assertLess(night, day / 3)

    def test_apply_clock_shift_rewrites_once_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "sim_abc123", "simulation_config.json")
            path.parent.mkdir()
            path.write_text(json.dumps(SCHEDULE_CONFIG))
            path.chmod(0o640)
            first = sim_schedule.apply_clock_shift("sim_abc123", 8, 30, simulations_dir=Path(tmp))
            self.assertTrue(first["changed"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(json.loads(path.read_text())[sim_schedule.CLOCK_MARKER], 8)
            second = sim_schedule.apply_clock_shift("sim_abc123", 8, 30, simulations_dir=Path(tmp))
            self.assertFalse(second["changed"])
            self.assertEqual(second["config_sha256_before"], first["config_sha256_after"])
            self.assertEqual(second["expected_agent_turns"], first["expected_agent_turns"])
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["simulation_config.json"])
            for bad in ("../sim_abc123", "sim_abc/../x", "proj_1"):
                with self.assertRaises(ValueError):
                    sim_schedule.apply_clock_shift(bad, 8, 30, simulations_dir=Path(tmp))

    def test_cost_model_from_pilot_schedule(self):
        schedule = sim_schedule.schedule_of(sim_schedule.shift_config(SCHEDULE_CONFIG, 8))
        stages = {"prepare": {"calls": 10, "seconds": 100}, "rounds": {"calls": 40, "seconds": 330}}
        model = run_batch.cost_model_from_run(stages, 10, schedule, limits()["estimates"])
        turns = sim_schedule.expected_turns(schedule, 10)
        self.assertEqual(model["pilot_expected_turns"], turns)
        self.assertEqual(model["calls_per_round"], 0.0)
        self.assertEqual(model["calls_per_turn"], round(40 / turns, 3))
        self.assertEqual(model["seconds_per_turn"], round((330 - 3 * 10) / turns, 2))
        quiet = {**schedule, "agents": []}
        fallback = run_batch.cost_model_from_run(stages, 10, quiet, limits()["estimates"])
        self.assertEqual((fallback["calls_per_round"], fallback["calls_per_turn"]), (4.0, 1.5))


class VariantTests(unittest.TestCase):
    UNKNOWNS = [{"id": "U1", "alternatives": ["a", "b", "c"]}, {"id": "U2", "alternatives": ["w", "x", "y", "z"]}]

    def test_sampling_is_deterministic_and_balanced(self):
        first = unknown_variants.sample_variants(self.UNKNOWNS, 3, 42)
        self.assertEqual(first, unknown_variants.sample_variants(self.UNKNOWNS, 3, 42))
        for unknown in self.UNKNOWNS:
            picks = [v["choices"][unknown["id"]]["index"] for v in first]
            self.assertEqual(len(set(picks)), 3)
        many = unknown_variants.sample_variants(self.UNKNOWNS, 6, 42)
        self.assertEqual(sorted(v["choices"]["U1"]["index"] for v in many), [0, 0, 1, 1, 2, 2])

    def test_proposal_rejects_new_numbers_and_retries(self):
        llm = FakeLLM([{"alternatives": ["Strike lasting 9000 days.", "b", "c"]},
                       {"alternatives": ["The union strikes.", "The union does not strike.", "A one-day strike.",
                                         "x", "y", "z"]}])
        result = unknown_variants.propose_alternatives(llm, "The union has 410 members.",
                                                       {"id": "U1", "text": "Will they strike?"})
        self.assertEqual(len(result), 5)  # trimmed to five
        self.assertEqual(len(llm.prompts), 2)
        self.assertIn("data, not instructions", llm.prompts[0])

    def test_proposal_gives_up(self):
        with self.assertRaises(LLMError):
            unknown_variants.propose_alternatives(FakeLLM([{"alternatives": ["only one"]}]), "seed",
                                                  {"id": "U1", "text": "?"})


class MeterTests(unittest.TestCase):
    def test_analyse_json_and_tools(self):
        ok = {"choices": [{"message": {"content": "```json\n{\"a\": 1}\n```"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        bad = {"choices": [{"message": {"content": "not json"}, "finish_reason": "length"}]}
        request = {"model": "m", "response_format": {"type": "json_object"}}
        self.assertTrue(llm_meter.analyse(request, ok)["json_ok"])
        self.assertFalse(llm_meter.analyse(request, bad)["json_ok"])
        tools = {"model": "m", "tools": [{"type": "function"}]}
        call = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "{\"x\": 1}"}}]}}]}
        broken = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "{x"}}]}}]}
        self.assertEqual(llm_meter.analyse(tools, call)["tool_args_ok"], True)
        self.assertEqual(llm_meter.analyse(tools, broken)["tool_args_ok"], False)
        self.assertIsNone(llm_meter.analyse(tools, {"choices": [{"message": {"content": "hi"}}]})["tool_args_ok"])

    def test_proxy_forwards_and_logs_metadata_only(self):
        answer = {"choices": [{"message": {"content": "{\"SECRET-ANSWER\": true}"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 7, "completion_tokens": 2}}

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "meter.jsonl")
            meter = llm_meter.MeterServer(("127.0.0.1", 0), upstream.server_address[:2], log, 10)
            threading.Thread(target=meter.serve_forever, daemon=True).start()
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                url = f"http://127.0.0.1:{meter.server_address[1]}/v1/chat/completions"
                payload = {"model": "m", "messages": [{"role": "user", "content": "SECRET-PROMPT"}],
                           "response_format": {"type": "json_object"}}
                request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                                 headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=10) as response:
                    self.assertEqual(json.loads(response.read()), answer)
                with opener.open(f"http://127.0.0.1:{meter.server_address[1]}/meter/health", timeout=10) as response:
                    self.assertEqual(response.status, 200)
            finally:
                meter.shutdown()
                meter.server_close()
                upstream.shutdown()
                upstream.server_close()
            text = log.read_text()
            self.assertNotIn("SECRET", text)
            entry = json.loads(text.splitlines()[0])
            self.assertEqual((entry["status"], entry["json_ok"], entry["prompt_tokens"]), (200, True, 7))
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_refuses_non_loopback(self):
        with self.assertRaises(ValueError):
            llm_meter.parse_host_port("10.0.0.2:11434")
        self.assertEqual(llm_meter.parse_host_port("[::1]:11434"), ("::1", 11434))


def make_simulation_dir(root: Path, simulation_id: str = "sim_1") -> None:
    sim = root / simulation_id
    sim.mkdir(parents=True)
    (sim / "simulation_config.json").write_text(json.dumps({"agent_configs": [
        {"agent_id": 0, "entity_name": "Tomas Reyes", "entity_type": "Councillor", "stance": "opposing"},
        {"agent_id": 1, "entity_name": "Elena Duarte", "entity_type": "Mayor", "stance": "supportive"},
    ]}))
    db = sqlite3.connect(sim / "reddit_simulation.db")
    db.executescript("""
        CREATE TABLE user (user_id INTEGER PRIMARY KEY, agent_id INTEGER, user_name TEXT, name TEXT, bio TEXT,
                           created_at DATETIME, num_followings INTEGER, num_followers INTEGER);
        CREATE TABLE post (post_id INTEGER PRIMARY KEY, user_id INTEGER, original_post_id INTEGER, content TEXT,
                           quote_content TEXT, created_at DATETIME, num_likes INTEGER, num_dislikes INTEGER,
                           num_shares INTEGER, num_reports INTEGER);
        CREATE TABLE comment (comment_id INTEGER PRIMARY KEY, post_id INTEGER, user_id INTEGER, content TEXT,
                              created_at DATETIME, num_likes INTEGER, num_dislikes INTEGER);
        CREATE TABLE trace (user_id INTEGER, created_at DATETIME, action TEXT, info TEXT);
        INSERT INTO user VALUES (1, 0, 'treyes', 'Tomas Reyes', '', '0', 0, 0), (2, 1, 'eduarte', 'Elena Duarte', '', '0', 0, 0);
        INSERT INTO post VALUES (1, 1, NULL, 'I will fight Proposal 14.', NULL, '1', 2, 0, 0, 0);
        INSERT INTO comment VALUES (1, 1, 1, 'Join the hearing.', '2', 0, 0);
        INSERT INTO trace VALUES (1, '1', 'create_post', '{}'), (2, '1', 'do_nothing', '{}');
    """)
    db.commit()
    db.close()


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.runs = root / "runs"
        self.sims = root / "simulations"
        make_simulation_dir(self.sims)
        self.limits_path = root / "limits.yaml"
        raw = copy.deepcopy(RAW_LIMITS)
        raw["rounds"].update(real_min=10, real_max=10)  # the fake server only accepts 10 rounds
        self.limits_path.write_text(json.dumps(raw))  # JSON is valid YAML
        (root / "seed.md").write_text(SEED)
        (root / "questions.md").write_text(QUESTIONS)
        (root / "terms.txt").write_text("person: Elena Duarte\nplace: Riverton\n")
        self.root = root
        self.patches = [mock.patch.object(prepare_inputs, "RUNS_DIR", self.runs),
                        mock.patch.object(prepare_inputs, "MAPPINGS_DIR", root / "mappings"),
                        mock.patch.object(run_batch, "env_values", lambda: {"MIROFISH_MEMORY_BACKEND": "local"})]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def prepare(self, terms=False):
        return prepare_inputs.prepare("b1", self.root / "seed.md", self.root / "questions.md",
                                      self.root / "terms.txt" if terms else None, rng_seed=3)

    def test_prepare_writes_private_linted_inputs(self):
        inputs = self.prepare()
        self.assertEqual([u["id"] for u in inputs["unknowns"]], ["U1", "U2"])
        self.assertEqual(len(inputs["lint"]["findings"]), 4)
        upload = (self.runs / "b1" / "inputs" / "seed.md").read_text()
        self.assertNotIn("ZEBRA-CANARY-7731", upload)
        requirement = (self.runs / "b1" / "inputs" / "Q1.requirement.txt").read_text()
        self.assertTrue(requirement.startswith("Will Proposal 14 pass?"))
        self.assertIn("SCENARIO ASSUMPTION", requirement)
        self.assertEqual((self.runs / "b1").stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            self.prepare()
        with self.assertRaises(ValueError):
            prepare_inputs.validate_batch_id("bad_id")

    def test_prepare_with_terms_pseudonymises_upload_only(self):
        inputs = self.prepare(terms=True)
        upload = (self.runs / "b1" / "inputs" / "seed.md").read_text()
        original = (self.runs / "b1" / "inputs" / "seed.original.md").read_text()
        self.assertNotIn("Duarte", upload)
        self.assertNotIn("Riverton", upload)
        self.assertIn("Duarte", original)
        mapping = Path(inputs["mapping_file"])
        self.assertEqual(mapping.stat().st_mode & 0o777, 0o600)
        self.assertEqual(mapping.parent, self.root / "mappings")

    def test_pilot_then_confirmed_runs_with_manifests_and_resume(self):
        self.prepare()
        llm = FakeLLM([lambda prompt: {"alternatives": ["Option one.", "Option two.", "Option three."]}])
        unknown_variants.propose("b1", llm=llm)
        server = FakeMiroFish()
        meter_log = self.root / "meter.jsonl"

        def fake_meter(path=None, start=0.0, end=None):
            rows = [{"ts": start, "path": "/v1/chat/completions", "status": 200, "json_mode": True, "json_ok": True},
                    {"ts": start, "path": "/v1/chat/completions", "status": 200, "json_mode": True, "json_ok": False}]
            return rows

        try:
            with mock.patch.object(run_batch, "read_meter", fake_meter):
                batch = run_batch.Batch("b1", client=mirofish_api.MiroFishClient(server.url), log=lambda m: None,
                                        limits_path=self.limits_path, meter_path=meter_log, runs_dir=self.runs,
                                        poll_interval_s=0.01, check_meter=False,
                                        provenance_fn=lambda: {"model": {"gguf_sha256": "abc"}},
                                        simulations_dir=self.sims)
                with self.assertRaises(run_batch.BatchError):
                    batch.run()  # no pilot yet
                pilot = batch.pilot()
                self.assertEqual(pilot["agents"], 12)
                self.assertEqual(pilot["cost_model"]["calls_per_round"], 0.0)
                self.assertGreater(pilot["cost_model"]["calls_per_turn"], 0)
                sim_config = json.loads((self.sims / "sim_1" / "simulation_config.json").read_text())
                self.assertEqual(sim_config[sim_schedule.CLOCK_MARKER], 8)
                self.assertEqual(sim_config["agent_configs"][0]["active_hours"], list(range(0, 15)))
                self.assertEqual(pilot["total"]["structured_failure_rate"], 0.5)
                results = batch.run()
                self.assertEqual(len(results), 4)
                self.assertEqual(server.count("/api/graph/ontology/generate"), 5)
                batch.run()  # resume: everything done, nothing re-submitted
                self.assertEqual(server.count("/api/graph/ontology/generate"), 5)
        finally:
            server.close()
        manifest = json.loads((self.runs / "b1_q1_r2" / "manifest.json").read_text())
        info = manifest["batch"]
        self.assertEqual(info["variant"]["index"], 1)
        self.assertEqual(info["provenance"]["model"]["gguf_sha256"], "abc")
        self.assertEqual(set(info["variant"]["choices"]), {"U1", "U2"})
        self.assertIn("rounds", info["metrics"]["stages"])
        # the fake server reuses one simulation, so later runs find it started and leave the clock alone
        self.assertIn("skipped", manifest["stages"]["schedule"]["result"])
        pilot_manifest = json.loads((self.runs / "b1_pilot" / "manifest.json").read_text())
        self.assertTrue(pilot_manifest["stages"]["schedule"]["result"]["changed"])
        uploads = [raw for _, p, raw in server.calls if p == "/api/graph/ontology/generate"]
        self.assertTrue(all(b"SCENARIO ASSUMPTION [U1]" in raw for raw in uploads))
        transcript = json.loads((self.runs / "b1_q1_r2" / "transcript.json").read_text())
        self.assertEqual(transcript["platforms"]["reddit"]["posts"][0]["content"], "I will fight Proposal 14.")
        state = json.loads((self.runs / "b1" / "batch.json").read_text())
        self.assertEqual(sorted(state["runs"]), ["b1_pilot", "b1_q1_r1", "b1_q1_r2", "b1_q2_r1", "b1_q2_r2"])
        self.assertEqual(purge.batch_run_ids("b1", self.runs), sorted(state["runs"]))

    def test_zep_cloud_backend_needs_a_pseudonymised_batch(self):
        server = FakeMiroFish()
        try:
            for terms, allowed in ((False, False), (True, True)):
                shutil.rmtree(self.runs / "b1", ignore_errors=True)
                self.prepare(terms=terms)
                batch = run_batch.Batch("b1", client=mirofish_api.MiroFishClient(server.url), log=lambda m: None,
                                        limits_path=self.limits_path, runs_dir=self.runs, check_meter=False)
                with mock.patch.object(run_batch, "env_values", lambda: {"MIROFISH_MEMORY_BACKEND": "zep"}):
                    if allowed:
                        batch.preflight()
                    else:
                        with self.assertRaisesRegex(run_batch.BatchError, "Zep Cloud"):
                            batch.preflight()
        finally:
            server.close()

    def test_killed_batch_resumes_without_repeating_finished_runs(self):
        self.prepare()
        unknown_variants.propose("b1", llm=FakeLLM([lambda p: {"alternatives": ["One.", "Two.", "Three."]}]))
        server = FakeMiroFish()
        prepared = []

        def log(message):
            if message.startswith("[prepare] done"):
                prepared.append(message)
                if len(prepared) == 2:  # b1_q1_r1 finishes; b1_q1_r2 is killed after its prepare stage
                    raise KeyboardInterrupt

        def make_batch(log_fn):
            return run_batch.Batch("b1", client=mirofish_api.MiroFishClient(server.url), log=log_fn,
                                   limits_path=self.limits_path, meter_path=self.root / "meter.jsonl",
                                   runs_dir=self.runs, poll_interval_s=0.01, check_meter=False,
                                   provenance_fn=lambda: {}, simulations_dir=self.sims)

        rows = lambda path=None, start=0.0, end=None: [{"ts": start, "path": "/v1/chat/completions", "status": 200}]
        try:
            with mock.patch.object(run_batch, "read_meter", rows):
                make_batch(lambda m: None).pilot()
                with self.assertRaises(KeyboardInterrupt):
                    make_batch(log).run()
                state = json.loads((self.runs / "b1" / "batch.json").read_text())
                self.assertEqual(state["runs"]["b1_q1_r1"]["status"], "done")
                self.assertEqual(state["runs"]["b1_q1_r2"]["status"], "failed")
                self.assertNotIn("b1_q2_r1", state["runs"])
                before = server.count("/api/graph/ontology/generate")
                resumed_log = []
                make_batch(resumed_log.append).run()
        finally:
            server.close()
        # resume: r1 skipped, r2 continues after its finished stages, the two Q2 runs start fresh
        self.assertEqual(server.count("/api/graph/ontology/generate") - before, 2)
        self.assertIn("b1_q1_r1: already done, skipping", resumed_log)
        self.assertTrue(any(m.startswith("[prepare] already done") for m in resumed_log))
        state = json.loads((self.runs / "b1" / "batch.json").read_text())
        self.assertEqual({r: v["status"] for r, v in state["runs"].items()},
                         {r: "done" for r in ("b1_pilot", "b1_q1_r1", "b1_q1_r2", "b1_q2_r1", "b1_q2_r2")})

    def test_summarize_calls_and_cost_model(self):
        rows = [{"path": "/v1/chat/completions", "status": 200, "tools": True, "tool_calls": 1, "tool_args_ok": True},
                {"path": "/v1/chat/completions", "status": 200, "tools": True, "tool_calls": 0},
                {"path": "/v1/chat/completions", "status": 500},
                {"path": "/api/embed", "status": 200}]
        summary = run_batch.summarize_calls(rows)
        self.assertEqual((summary["calls"], summary["http_errors"], summary["tool_calls_missing"]), (3, 1, 1))
        self.assertEqual(summary["structured_failure_rate"], 0.0)
        self.assertEqual(summary["no_action_rate"], 0.5)
        model = run_batch.cost_model_from_run({"ontology": {"calls": 4, "seconds": 10},
                                               "rounds": {"calls": 50, "seconds": 100}}, 10)
        self.assertEqual(model, {"fixed_calls": 4, "calls_per_round": 5.0, "fixed_seconds": 10, "seconds_per_round": 10.0})


REFERENCE = seedfile.lint_seed(SEED)[0]


class FactGuardTests(unittest.TestCase):
    REPORT = ("# Future Prediction Report\n\nOn 3 March 2026 the council published Proposal 14. The city expects "
              "savings of $3.2 million annually. Mayor Elena Duarte posted that the vote on 21 April 2026 was "
              "unanimous. The Northgate Freight Alliance commented on the plan. ZEBRA-CANARY-7731 posted support. "
              "A second vote happened on 5 May 2026. About 30 percent of staff live nearby. The petition reached "
              "1,800 signatures, later 2,400. In 2027 the depot closed.\n")

    def types(self, flags, kind):
        return [f["value"] for f in flags if f["type"] == kind]

    def test_numbers_dates_entities(self):
        self.assertEqual(self.types(factguard.check_numbers(self.REPORT, REFERENCE), "UNSUPPORTED_NUMBER"),
                         ["$3.2 million", "2,400"])
        self.assertEqual(self.types(factguard.check_dates(self.REPORT, REFERENCE), "UNSUPPORTED_DATE"),
                         ["5 May 2026", "2027"])
        entities = self.types(factguard.check_entities(self.REPORT, REFERENCE), "UNSUPPORTED_ENTITY")
        self.assertIn("Northgate Freight Alliance", entities)
        self.assertIn("ZEBRA-CANARY-7731", entities)
        self.assertNotIn("Elena Duarte", entities)
        self.assertNotIn("Future Prediction Report", entities)  # headings are skipped

    def test_bold_section_titles_are_headings(self):
        report = ("**Emergent Information Ecosystem**\n\nThe Northgate Freight Alliance is **Very Loud** today.\n")
        entities = self.types(factguard.check_entities(report, REFERENCE), "UNSUPPORTED_ENTITY")
        self.assertNotIn("Emergent Information Ecosystem", entities)
        self.assertIn("Northgate Freight Alliance", entities)
        self.assertNotIn("Emergent", " ".join(factguard.sentences(report)))

    def test_transcript_posts_are_checked(self):
        transcript = {"agents": [{"agent_id": 0, "entity_name": "Tomas Reyes"}], "platforms": {"reddit": {
            "users": [{"user_id": 1, "agent_id": 0, "name": "treyes_1"}],
            "posts": [{"post_id": 1, "user_id": 1, "quote_content": None,
                       "content": "Maria Gonzalez says 2,400 people signed. #RivertonNeedsItsDepot Tomas Reyes agrees."}],
            "comments": [{"comment_id": 4, "post_id": 1, "user_id": 1,
                          "content": "Maria Gonzalez again: 2,400! Thanks @RivertonDaily, see you by 21 November."}]}}}
        flags = factguard.check_transcript(transcript, REFERENCE)
        values = [(f["type"], f["value"]) for f in flags]
        self.assertEqual(values, [("UNSUPPORTED_NUMBER", "2,400"), ("UNSUPPORTED_ENTITY", "Maria Gonzalez")])
        self.assertEqual(flags[0]["where"], "reddit post 1 by Tomas Reyes")
        self.assertNotIn("spans", flags[0])

    def test_annotated_report_marks_values_and_sentences(self):
        flags = (factguard.check_numbers(self.REPORT, REFERENCE) + factguard.check_dates(self.REPORT, REFERENCE)
                 + factguard.check_actors(self.REPORT, {"Elena Duarte": 0}))
        text = factguard.annotate(self.REPORT, flags, "r1")
        self.assertIn("2,400 **[UNSUPPORTED_NUMBER]**", text)
        self.assertIn("5 May 2026 **[UNSUPPORTED_DATE]**", text)
        self.assertIn("was unanimous. **[INACTIVE_ACTOR]**", text)
        self.assertNotIn("30 percent **[", text)  # supported values are left alone
        body = text.split("\n\n", 1)[1]
        self.assertEqual(re.sub(r" \*\*\[[A-Z_0-9 ]+\]\*\*", "", body), self.REPORT)

    def test_inactive_actor(self):
        flags = factguard.check_actors(self.REPORT, {"Elena Duarte": 0, "Tomas Reyes": 3})
        self.assertEqual(self.types(flags, "INACTIVE_ACTOR"), ["Elena Duarte"])

    def test_agent_activity_from_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_simulation_dir(Path(tmp))
            run_batch.export_transcript("sim_1", Path(tmp, "t.json"), Path(tmp))
            activity = factguard.agent_activity(json.loads(Path(tmp, "t.json").read_text()))
        self.assertEqual(activity, {"Tomas Reyes": 2, "Elena Duarte": 0})

    def test_unknown_stance_judged_against_variant(self):
        report = "Councillor Reyes spoke. The union will call a strike as soon as Proposal 14 passes."
        unknowns = [{"id": "U1", "text": "Whether the union will call a strike if Proposal 14 passes."}]
        mismatch = FakeLLM([{"resolves": True, "matches_premise": False}])
        choices = {"U1": {"index": 1, "text": "The union does not strike."}}
        flags = factguard.check_unknowns(report, unknowns, choices, mismatch)
        self.assertEqual([f["type"] for f in flags], ["UNKNOWN_STANCE"])
        self.assertIn("call a strike", flags[0]["sentence"])
        match = FakeLLM([{"resolves": True, "matches_premise": True}])
        self.assertEqual(factguard.check_unknowns(report, unknowns, {"U1": {"text": "They strike."}}, match), [])
        self.assertEqual(len(factguard.check_unknowns(report, unknowns, {}, match)), 1)  # no premise: any answer flagged
        review = factguard.check_unknowns(report, unknowns, choices, None)
        self.assertEqual([f["type"] for f in review], ["UNKNOWN_REVIEW"])


class CompareTests(unittest.TestCase):
    RUBRIC = {"trajectories": [{"id": "rejection", "description": "x"}, {"id": "stalemate", "description": "y"}],
              "resistors": "agents who push back"}

    def transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_simulation_dir(Path(tmp))
            run_batch.export_transcript("sim_1", Path(tmp, "t.json"), Path(tmp))
            return json.loads(Path(tmp, "t.json").read_text())

    def test_digest_and_classify(self):
        transcript = self.transcript()
        text = compare.digest(transcript)
        self.assertIn("Tomas Reyes [Councillor] stance=opposing activity=2", text)
        self.assertIn("[reddit post] Tomas Reyes: I will fight Proposal 14.", text)
        llm = FakeLLM([{"trajectory": "nonsense"},
                       {"trajectory": "rejection", "resistors": ["Tomas Reyes", "Invented Person"], "rationale": "r"}])
        label = compare.classify(llm, self.RUBRIC, "Will it pass?", transcript, seed=0)
        self.assertEqual(label["trajectory"], "rejection")
        self.assertEqual(label["resistors"], ["Tomas Reyes"])
        self.assertEqual(label["unlisted_resistors"], ["Invented Person"])

    def test_aggregate_tables_and_noise_warnings(self):
        def run(run_id, variant, labels):
            return {"run_id": run_id, "question": "Q1", "variant": {"U1": {"index": variant, "text": f"alt{variant}"}},
                    "labels": [{"trajectory": t, "resistors": ["Tomas Reyes"]} for t in labels]}
        runs = [run("a", 0, ["rejection", "rejection"]), run("b", 1, ["stalemate", "rejection"])]
        summary = compare.aggregate(runs, [{"id": "U1", "text": "strike?"}])["Q1"]
        self.assertEqual(summary["trajectories"], {"rejection": 1, "stalemate": 1})
        self.assertEqual(summary["resistors"], {"Tomas Reyes": 2})
        self.assertEqual(summary["unknown_tables"]["U1"]["rows"], {"[0] alt0": {"rejection": 1}, "[1] alt1": {"stalemate": 1}})
        warnings = " ".join(summary["warnings"])
        self.assertIn("anecdotes", warnings)
        self.assertIn("disagreed with itself on 1", warnings)
        self.assertIn("cannot be separated", warnings)
        self.assertNotIn("mostly model noise", warnings)

    def test_identical_seed_runs_that_disagree_are_called_noise(self):
        def run(run_id, trajectory, rounds=30):
            return {"run_id": run_id, "question": "Q1", "variant": {}, "rounds": rounds,
                    "labels": [{"trajectory": trajectory, "resistors": []}]}
        agreeing = compare.aggregate([run("a", "rejection"), run("b", "rejection"), run("p", "stalemate", 10)], [])
        self.assertNotIn("model noise", " ".join(agreeing["Q1"]["warnings"]))
        warnings = " ".join(compare.aggregate([run("a", "rejection"), run("b", "stalemate")], [])["Q1"]["warnings"])
        self.assertIn("(a, b) ended differently (rejection, stalemate): results are mostly model noise", warnings)
        self.assertNotIn("cannot be separated", warnings)

    def test_shipped_rubric_is_valid(self):
        self.assertGreaterEqual(len(compare.load_rubric()["trajectories"]), 2)


if __name__ == "__main__":
    unittest.main()
