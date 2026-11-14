"""Tests for ops/scripts/doctor_llm.py against a fake Ollama server on loopback.

Run: python3 -m unittest discover -s ops/tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doctor_llm  # noqa: E402

GOOD_ANSWERS = {
    "entity types": {"entity_types": [
        {"name": "Council", "description": "Local government body"},
        {"name": "Union", "description": "Labour union"},
        {"name": "ParentGroup", "description": "Parents' association"},
    ]},
    "persona": {"bio": "Retired teacher.", "persona": "Kind and civic-minded.", "age": 68,
                "gender": "female", "mbti": "ISFJ"},
    "timing": {"total_simulation_hours": 72, "minutes_per_round": 60, "peak_hours": [19, 20, 21]},
    "Mayor Dana Ortiz": {
        "entities": [{"name": "Dana Ortiz", "type": "Person"}, {"name": "Riverton Council", "type": "Organization"},
                     {"name": "Sam Lee", "type": "Person"}],
        "relations": [{"source": "Dana Ortiz", "target": "Riverton Council", "type": "LEADS",
                       "fact": "Dana Ortiz is mayor and speaks for the council."}],
    },
    "Classify": {"stance": "opposing", "confidence": 0.9, "reason": "Argues closure harms children."},
}


def make_handler(*, num_ctx: int = 32768, json_mode: str = "good"):
    class FakeOllama(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence test output
            pass

        def _send(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/tags":
                self._send({"models": [{"name": "mirofish-llm:latest", "digest": "abc123def4567890",
                                        "size": 18_600_000_000}]})
            else:
                self.send_error(404)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                self._send({"parameters": f"num_ctx                        {num_ctx}",
                            "details": {"parameter_size": "30.5B", "quantization_level": "Q4_K_M",
                                        "family": "qwen3moe"}})
            elif self.path == "/v1/chat/completions":
                prompt = payload["messages"][-1]["content"]
                answer = next(v for k, v in GOOD_ANSWERS.items() if k in prompt)
                if json_mode == "bad":
                    content = "Sure! Here is your answer: " + json.dumps(answer)[:20]
                elif json_mode == "fenced_think":
                    content = "<think>planning</think>\n```json\n" + json.dumps(answer) + "\n```"
                else:
                    content = json.dumps(answer)
                self._send({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
            elif self.path == "/api/chat":
                self._send({"message": {"content": "text"}, "eval_count": 200, "eval_duration": 4_000_000_000,
                            "prompt_eval_count": 40, "prompt_eval_duration": 100_000_000})
            else:
                self.send_error(404)

    return FakeOllama


class DoctorLlmTests(unittest.TestCase):
    def _run(self, handler, extra_args=()):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                profile = Path(tmp) / "state" / "hardware_profile.json"
                out = io.StringIO()
                with redirect_stdout(out):
                    code = doctor_llm.main([
                        "--host", f"127.0.0.1:{server.server_address[1]}",
                        "--model", "mirofish-llm",
                        "--memory-budget-gb", "72",
                        "--profile-out", str(profile),
                        "--timeout", "10",
                        *extra_args,
                    ])
                data = json.loads(profile.read_text()) if profile.exists() else None
                return code, out.getvalue(), data
        finally:
            server.shutdown()
            server.server_close()

    def test_all_checks_pass_and_profile_written(self):
        code, out, profile = self._run(make_handler())
        self.assertEqual(code, 0, out)
        self.assertIn("structured JSON compliance 5/5", out)
        self.assertEqual(profile["json_compliance"]["passed"], 5)
        self.assertEqual(profile["benchmark"]["gen_tokens_per_sec"], 50.0)
        self.assertEqual(profile["model"]["num_ctx"], 32768)

    def test_think_tags_and_fences_are_tolerated_like_mirofish(self):
        code, out, _ = self._run(make_handler(json_mode="fenced_think"), ["--skip-bench"])
        self.assertEqual(code, 0, out)

    def test_non_json_output_fails(self):
        code, out, profile = self._run(make_handler(json_mode="bad"), ["--skip-bench"])
        self.assertEqual(code, 1)
        self.assertIn("structured JSON compliance 0/5", out)
        self.assertEqual(profile["json_compliance"]["passed"], 0)

    def test_small_context_fails(self):
        code, out, _ = self._run(make_handler(num_ctx=4096), ["--skip-bench"])
        self.assertEqual(code, 1)
        self.assertIn("num_ctx is 4096", out)

    def test_refuses_non_loopback_host(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor_llm.main(["--host", "10.0.0.5:11434", "--model", "m"])
        self.assertEqual(code, 1)
        self.assertIn("not loopback", out.getvalue())

    def test_schema_checks_reject_wrong_types(self):
        self.assertFalse(doctor_llm._check_time_config({"total_simulation_hours": "72", "minutes_per_round": 60,
                                                        "peak_hours": [19]}))
        self.assertFalse(doctor_llm._check_stance({"stance": "opposing", "confidence": True, "reason": "x"}))
        self.assertFalse(doctor_llm._check_extraction({"entities": [{"name": "A", "type": "P"}, {"name": "B", "type": "P"}],
                                                       "relations": [{"source": "A", "target": "C", "type": "R",
                                                                      "fact": "f"}]}))

    def test_parse_num_ctx(self):
        self.assertEqual(doctor_llm.parse_num_ctx("stop \"<|im_end|>\"\nnum_ctx 32768\n"), 32768)
        self.assertIsNone(doctor_llm.parse_num_ctx("temperature 0.7"))


if __name__ == "__main__":
    unittest.main()
