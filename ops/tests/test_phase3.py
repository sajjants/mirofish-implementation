"""Tests for the Phase 3 wrapper: envfile, netcheck evaluation, purge, the REST client,
the network-audit hook and the offline tokenizer shim. No real MiroFish or LLM needed.

Run: python3 -m unittest discover -s ops/tests -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

OPS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_DIR / "scripts"))
import envfile  # noqa: E402
import injection_check  # noqa: E402
import mirofish_api  # noqa: E402
import netcheck  # noqa: E402
import purge  # noqa: E402

BACKEND_PYTHON = OPS_DIR.parent / "mirofish" / "backend" / ".venv" / "bin" / "python"


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_adds_only_missing_keys_and_keeps_values(self):
        example = self.dir / "example"
        target = self.dir / ".env"
        example.write_text("# comment\nA=1\nB=2\nC=3\n")
        target.write_text("A=keep\nB=keep")
        added = envfile.sync(example, target)
        self.assertEqual(added, ["C"])
        self.assertEqual(target.read_text(), "A=keep\nB=keep\nC=3\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_set_replaces_and_appends(self):
        target = self.dir / ".env"
        target.write_text("A=1\nB=2\n")
        envfile.set_values(target, {"B": "/path with spaces", "D": "4"})
        self.assertEqual(envfile.parse(envfile.read_lines(target)), {"A": "1", "B": "/path with spaces", "D": "4"})

    def test_parse_matches_python_dotenv_rules(self):
        lines = ["export A=1\n", 'B="quoted # not a comment"\n', "C=value # comment\n", "A=last\n"]
        self.assertEqual(envfile.parse(lines), {"A": "last", "B": "quoted # not a comment", "C": "value"})

    def test_set_replaces_every_duplicate(self):
        target = self.dir / ".env"
        target.write_text("HF_HUB_OFFLINE=0\nX=1\nexport HF_HUB_OFFLINE=0\n")
        envfile.set_values(target, {"HF_HUB_OFFLINE": "1"})
        self.assertEqual(target.read_text(), "HF_HUB_OFFLINE=1\nX=1\nHF_HUB_OFFLINE=1\n")

    def test_set_rejects_newlines_and_bad_keys(self):
        target = self.dir / ".env"
        target.write_text("A=1\n")
        with self.assertRaises(ValueError):
            envfile.set_values(target, {"A": "x\nEVIL=1"})
        with self.assertRaises(ValueError):
            envfile.set_values(target, {"1BAD": "x"})
        self.assertEqual(target.read_text(), "A=1\n")


class NetcheckTests(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "127.8.8.8", "::1", "[::1]", "::ffff:127.0.0.1", "localhost", None):
            self.assertTrue(netcheck.is_loopback(host), host)
        for host in ("10.0.0.1", "192.168.1.5", "8.8.8.8", "2001:db8::1", "huggingface.co", "0.0.0.0"):
            self.assertFalse(netcheck.is_loopback(host), host)

    def test_audit_flags_remote_connects_and_dns_but_not_unix_or_loopback(self):
        records = [
            {"event": "socket.connect", "family": "AF_INET", "host": "127.0.0.1", "port": 11434},
            {"event": "socket.getaddrinfo", "family": "dns", "host": "localhost", "port": 5001},
            {"event": "socket.connect", "family": "unix", "host": None, "port": None},
            {"event": "socket.getaddrinfo", "family": "dns", "host": "huggingface.co", "port": 443},
            {"event": "socket.connect", "family": "AF_INET", "host": "140.82.112.3", "port": 443},
        ]
        bad = netcheck.evaluate_audit(records, set(), [])
        self.assertEqual([r["host"] for r in bad], ["huggingface.co", "140.82.112.3"])

    def test_allowlist_accepts_names_and_cidrs(self):
        names = {"api.getzep.com", "203.0.113.0/24"}
        networks = netcheck.resolve_allowlist({"203.0.113.0/24"})
        self.assertTrue(netcheck.is_allowed("api.getzep.com", names, networks))
        self.assertTrue(netcheck.is_allowed("203.0.113.9", names, networks))
        self.assertFalse(netcheck.is_allowed("198.51.100.1", names, networks))

    def test_load_allowlist_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("# header\n\napi.getzep.com  # zep\n")
        try:
            self.assertEqual(netcheck.load_allowlist(Path(handle.name)), {"api.getzep.com"})
        finally:
            os.unlink(handle.name)

    def test_lsof_parsing_and_evaluation(self):
        parsed = netcheck.parse_lsof_name("127.0.0.1:50122->127.0.0.1:11434 (ESTABLISHED)")
        self.assertEqual(parsed["remote_host"], "127.0.0.1")
        self.assertEqual(netcheck.parse_lsof_name("[::1]:5001->[::1]:6000")["remote_host"], "::1")
        samples = [
            {"group": "mirofish", "command": "python", "name": "127.0.0.1:5001"},
            {"group": "mirofish", "command": "python", "name": "127.0.0.1:5001->127.0.0.1:60000"},
            {"group": "mirofish", "command": "node", "name": "*:3000"},
            {"group": "mirofish", "command": "python", "name": "10.0.0.2:5555->151.101.1.1:443"},
            {"group": "mirofish", "command": "python", "name": "10.0.0.2:5555->151.101.1.1:443"},
            {"group": "mirofish", "command": "python", "proto": "UDP", "name": "*:*"},
            {"group": "ollama", "command": "ollama", "name": "127.0.0.1:11434->127.0.0.1:50000"},
            {"group": "ollama", "command": "ollama", "name": "10.0.0.2:6000->34.36.133.15:443"},
        ]
        bad = netcheck.evaluate_lsof(samples, {"34.36.133.15"}, [])
        self.assertEqual([s["name"] for s in bad],
                         ["*:3000", "10.0.0.2:5555->151.101.1.1:443", "10.0.0.2:6000->34.36.133.15:443"])

    def test_missing_evidence_blocks_a_pass(self):
        backend = {"event": "socket.connect", "family": "AF_INET", "host": "127.0.0.1", "port": 11434,
                   "proc": "run.py"}
        sim = dict(backend, proc="run_parallel_simulation.py")
        sample = [{"group": "mirofish", "name": "127.0.0.1:5001"}]
        self.assertEqual(netcheck.missing_evidence([backend, sim], sample, True), [])
        self.assertEqual(len(netcheck.missing_evidence([backend], sample, True)), 1)
        self.assertEqual(len(netcheck.missing_evidence([], [], False)), 2)

    def test_evaluate_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit, lsof, allowed = Path(tmp, "a.jsonl"), Path(tmp, "l.jsonl"), Path(tmp, "allowed.txt")
            allowed.write_text("# none\n")
            audit.write_text(json.dumps({"event": "socket.connect", "family": "AF_INET",
                                         "host": "127.0.0.1", "port": 11434}) + "\n")
            audit.write_text(audit.read_text().replace('"port": 11434}', '"port": 11434, "proc": "run.py"}'))
            lsof.write_text(json.dumps({"group": "mirofish", "command": "python",
                                        "name": "127.0.0.1:5001"}) + "\n")
            args = ["evaluate", "--audit", str(audit), "--lsof", str(lsof), "--allowed", str(allowed)]
            with mock.patch("sys.stdout"):
                self.assertEqual(netcheck.main(args), 0)
                with open(audit, "a") as handle:
                    handle.write(json.dumps({"event": "socket.connect", "family": "AF_INET6",
                                             "host": "2606:4700::1", "port": 443}) + "\n")
                self.assertEqual(netcheck.main(args + ["--report", str(Path(tmp, "r.json"))]), 1)
            report = json.loads(Path(tmp, "r.json").read_text())
            self.assertFalse(report["passed"])
            self.assertEqual(Path(tmp, "r.json").stat().st_mode & 0o777, 0o600)
            with mock.patch("sys.stdout"):
                self.assertEqual(netcheck.main(args[:5] + ["--lsof", str(Path(tmp, "empty.jsonl"))]), 1)


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_checked_child_rejects_traversal_and_symlinks(self):
        base = self.root / "projects"
        base.mkdir()
        for bad in ("../x", "a/b", "..", ".hidden", "x" * 81, "proj 1"):
            with self.assertRaises(ValueError, msg=bad):
                purge.checked_child(base, bad)
        (base / "link").symlink_to(self.root)
        with self.assertRaises(ValueError):
            purge.checked_child(base, "link")
        self.assertEqual(purge.checked_child(base, "proj_abc"), base / "proj_abc")
        self.assertIsNone(purge.checked_child(base, None))

    def test_shred_overwrites_content_before_unlinking(self):
        target = self.root / "seed.txt"
        payload = b"SECRET-SEED-CONTENT" * 1000
        target.write_bytes(payload)
        fd = os.open(target, os.O_RDONLY)  # an open descriptor still sees the inode after unlink
        try:
            purge.shred_file(target)
            self.assertFalse(target.exists())
            self.assertEqual(os.pread(fd, len(payload), 0), b"\0" * len(payload))
        finally:
            os.close(fd)

    def test_shred_does_not_zero_hard_linked_files(self):
        target = self.root / "seed.txt"
        target.write_text("shared")
        other = self.root / "other-name"
        os.link(target, other)
        warnings = []
        purge.shred_file(target, warnings)
        self.assertFalse(target.exists())
        self.assertEqual(other.read_text(), "shared")
        self.assertIn("hard links", warnings[0])

    def test_shred_tree_does_not_follow_symlinks(self):
        outside = self.root / "outside.txt"
        outside.write_text("keep me")
        tree = self.root / "tree"
        (tree / "sub").mkdir(parents=True)
        (tree / "sub" / "a.txt").write_text("a")
        (tree / "escape").symlink_to(outside)
        (tree / "dirlink").symlink_to(self.root)
        self.assertEqual(purge.shred_tree(tree), 2)  # a.txt and the file symlink (unlinked, not followed)
        self.assertFalse(tree.exists())
        self.assertEqual(outside.read_text(), "keep me")
        self.assertTrue(self.root.is_dir())

    def test_purge_removes_run_outputs_with_backend_stopped(self):
        ops, uploads = self.root / "ops", self.root / "uploads"
        run_dir = ops / "runs" / "run1"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({"project_id": "proj_1", "simulation_id": "sim_1"}))
        for sub in ("projects/proj_1/files", "simulations/sim_1/twitter", "reports/report_1",
                    "reports/report_other", "projects/proj_2", "projects/proj_orphan"):
            (uploads / sub).mkdir(parents=True)
            (uploads / sub / "data.txt").write_text("seed-derived")
        # Not in the manifest: a report for this run's simulation, and a project named after the run.
        (uploads / "reports/report_1/meta.json").write_text(json.dumps({"simulation_id": "sim_1"}))
        (uploads / "reports/report_other/meta.json").write_text(json.dumps({"simulation_id": "sim_9"}))
        (uploads / "projects/proj_orphan/project.json").write_text(json.dumps({"name": "run1"}))
        client = mock.Mock()
        client.health.return_value = False
        with mock.patch.object(purge, "OPS_DIR", ops), mock.patch.object(purge, "UPLOADS", uploads):
            done = purge.purge("run1", include_logs=False, client=client, log=lambda _: None)
        self.assertFalse(run_dir.exists())
        for gone in ("projects/proj_1", "projects/proj_orphan", "simulations/sim_1", "reports/report_1"):
            self.assertFalse((uploads / gone).exists(), gone)
        self.assertTrue((uploads / "projects/proj_2/data.txt").exists())
        self.assertTrue((uploads / "reports/report_other/data.txt").exists())
        self.assertTrue(any("simulation sim_1" in line for line in done))

    def test_failed_graph_delete_keeps_the_manifest(self):
        ops = self.root / "ops"
        (ops / "runs" / "run1").mkdir(parents=True)
        (ops / "runs" / "run1" / "manifest.json").write_text(json.dumps({"graph_id": "mirofish_abc"}))
        client = mock.Mock()
        client.health.return_value = True
        client.get.return_value = []
        client.delete.side_effect = mirofish_api.ApiError("DELETE ... -> HTTP 409: busy")
        with mock.patch.object(purge, "OPS_DIR", ops), mock.patch.object(purge, "UPLOADS", self.root / "u"):
            with self.assertRaises(RuntimeError):
                purge.purge("run1", include_logs=False, client=client, log=lambda _: None)
            self.assertTrue((ops / "runs" / "run1" / "manifest.json").exists())
            client.delete.side_effect = mirofish_api.ApiError("DELETE ... -> HTTP 404: not found")
            done = purge.purge("run1", include_logs=False, client=client, log=lambda _: None)
        self.assertIn("graph mirofish_abc: already gone", done)

    def test_purge_refuses_while_simulation_runs_and_logs_while_running(self):
        ops = self.root / "ops"
        (ops / "runs" / "run1").mkdir(parents=True)
        (ops / "runs" / "run1" / "manifest.json").write_text(json.dumps({"simulation_id": "sim_1"}))
        client = mock.Mock()
        client.health.return_value = True
        client.get.return_value = {"runner_status": "running"}
        with mock.patch.object(purge, "OPS_DIR", ops), mock.patch.object(purge, "UPLOADS", self.root / "u"):
            with self.assertRaises(RuntimeError):
                purge.purge("run1", include_logs=False, client=client, log=lambda _: None)
            client.get.side_effect = lambda path: ([{"status": "processing"}] if path == "/api/graph/tasks"
                                                   else {"runner_status": "completed"})
            with self.assertRaises(RuntimeError):
                purge.purge("run1", include_logs=False, client=client, log=lambda _: None)
            client.get.side_effect = lambda path: [] if path == "/api/graph/tasks" else {"runner_status": "completed"}
            with self.assertRaises(RuntimeError):
                purge.purge("run1", include_logs=True, client=client, log=lambda _: None)
        self.assertTrue((ops / "runs" / "run1" / "manifest.json").exists())


class FakeMiroFish:
    """Just enough of the MiroFish API for one run, on a loopback port."""

    def __init__(self, fail_report_once: bool = False, expected_agents: int = 12):
        self.calls = []
        self.fail_report = fail_report_once
        self.closed = False
        self.started = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, data, success=True, status=200):
                body = json.dumps({"success": success, "data": data} if success
                                  else {"success": False, "error": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append(("GET", self.path, None))
                if self.path == "/health":
                    return self._reply({"status": "ok"})
                if self.path == "/api/graph/task/t1":
                    return self._reply({"status": "completed", "result": {"graph_id": "g1", "node_count": 9}})
                if self.path == "/api/simulation/sim_1/run-status":
                    if not fake.started:
                        return self._reply({"runner_status": "idle"})
                    return self._reply({"runner_status": "completed" if fake.closed else "running",
                                        "reddit_completed": True, "twitter_completed": True,
                                        "current_round": 10, "total_rounds": 10})
                if self.path == "/api/report/r1":
                    return self._reply({"markdown_content": "# Report\nbody"})
                return self._reply("not found", success=False, status=404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                fake.calls.append(("POST", self.path, raw))
                if self.path == "/api/graph/ontology/generate":
                    return self._reply({"project_id": "proj_1"})
                body = json.loads(raw or b"{}")
                routes = {
                    "/api/graph/build": {"task_id": "t1"},
                    "/api/simulation/create": {"simulation_id": "sim_1"},
                    "/api/simulation/prepare": {"task_id": "t2", "expected_entities_count": expected_agents},
                    "/api/simulation/prepare/status": {"status": "completed"},
                    "/api/simulation/start": {"runner_status": "running"},
                    "/api/report/generate": {"report_id": "r1", "task_id": "t3"},
                }
                if self.path == "/api/simulation/close-env":
                    fake.closed = True
                    return self._reply({"message": "closed"})
                if self.path == "/api/report/generate/status":
                    if fake.fail_report:
                        fake.fail_report = False
                        return self._reply({"status": "failed", "error": "LLM timeout"})
                    return self._reply({"status": "completed", "result": {"report_id": "r1"}})
                if self.path in routes:
                    if self.path == "/api/simulation/start":
                        assert body["max_rounds"] == 10 and body["platform"] == "parallel"
                        assert body["force"] is False
                        fake.started = True
                    return self._reply(routes[self.path])
                return self._reply("not found", success=False, status=404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, path):
        return sum(1 for _, p, _ in self.calls if p == path)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ApiClientTests(unittest.TestCase):
    def test_refuses_non_loopback_urls(self):
        for url in ("http://10.0.0.5:5001", "http://example.com", "http://127.0.0.1.evil.com:5001"):
            with self.assertRaises(ValueError):
                mirofish_api.MiroFishClient(url)

    def test_run_id_validation(self):
        for good in ("pilot-1", "q1_r2.v3"):
            self.assertEqual(mirofish_api.validate_run_id(good), good)
        for bad in ("", "../x", "a/b", ".hidden", "a..b", "x" * 65, "run1\n"):
            with self.assertRaises(ValueError):
                mirofish_api.validate_run_id(bad)

    def test_rounds_done_by_platform(self):
        self.assertTrue(mirofish_api.rounds_done({"reddit_completed": True}, "reddit"))
        self.assertFalse(mirofish_api.rounds_done({"reddit_completed": True}, "parallel"))
        self.assertTrue(mirofish_api.rounds_done({"runner_status": "completed"}, "parallel"))

    def test_pipeline_runs_writes_private_manifest_and_resumes(self):
        server = FakeMiroFish(fail_report_once=True)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runs = Path(tmp, "runs")
                seed = Path(tmp, "seed.md")
                seed.write_text("Mayor Dana Ortiz supports the plan.")
                client = mirofish_api.MiroFishClient(server.url)
                logs = []
                pipeline = mirofish_api.RunPipeline(client, "r1", runs_dir=runs, log=logs.append,
                                                    poll_interval_s=0.01)
                kwargs = dict(max_rounds=10, platform="parallel", max_agents=10)
                with self.assertRaises(mirofish_api.ApiError):
                    pipeline.run([seed], "How will residents react?", **kwargs)
                manifest = json.loads((runs / "r1" / "manifest.json").read_text())
                self.assertEqual(manifest["stages"]["report"]["status"], "failed")
                self.assertEqual(manifest["report_id"], "r1")  # recorded before the report finished
                self.assertEqual(manifest["stages"]["rounds"]["status"], "done")
                self.assertTrue(any("12 agents exceeds the cap of 10" in line for line in logs))

                resumed = mirofish_api.RunPipeline(client, "r1", runs_dir=runs, log=logs.append,
                                                   poll_interval_s=0.01)
                result = resumed.run([seed], "How will residents react?", **kwargs)
                self.assertEqual(result["report_id"], "r1")
                self.assertEqual(server.count("/api/graph/ontology/generate"), 1)
                self.assertEqual(server.count("/api/simulation/start"), 1)
                self.assertEqual((runs / "r1" / "report.md").read_text(), "# Report\nbody")
                self.assertEqual((runs / "r1").stat().st_mode & 0o777, 0o700)
                self.assertEqual((runs / "r1" / "manifest.json").stat().st_mode & 0o777, 0o600)
                upload = next(raw for _, p, raw in server.calls if p == "/api/graph/ontology/generate")
                self.assertIn(b"How will residents react?", upload)
                self.assertIn(b'filename="seed.md"', upload)

                with self.assertRaises(mirofish_api.ApiError):
                    mirofish_api.RunPipeline(client, "r1", runs_dir=runs, log=logs.append).run(
                        [seed], "A different question", **kwargs)
        finally:
            server.close()

    def test_rounds_ceiling(self):
        pipeline = mirofish_api.RunPipeline(mock.Mock(), "x", runs_dir=Path(tempfile.gettempdir()))
        with self.assertRaises(ValueError):
            pipeline.run([], "q", max_rounds=41)

    def test_rounds_stall_is_reported(self):
        client = mock.Mock()
        client.post_multipart.return_value = {"project_id": "p"}
        responses = {"/api/graph/task/t": {"status": "completed", "result": {"graph_id": "g"}},
                     "/api/simulation/s/run-status": {"runner_status": "running", "current_round": 0,
                                                      "total_actions_count": 0}}
        client.get.side_effect = lambda path, **kw: responses[path]
        client.post.side_effect = lambda path, payload, **kw: {
            "/api/graph/build": {"task_id": "t"}, "/api/simulation/create": {"simulation_id": "s"},
            "/api/simulation/prepare": {"already_prepared": True}, "/api/simulation/start": {}}[path]
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = mirofish_api.RunPipeline(client, "stall", runs_dir=Path(tmp), log=lambda m: None,
                                                poll_interval_s=0.01)
            with self.assertRaisesRegex(mirofish_api.ApiError, "no progress for 0s"):
                pipeline.run([], "q", max_rounds=10, stall_timeout_s=0.05)

    def test_wrong_agent_count_after_prepare_fails_and_rerun_regenerates(self):
        def write_prepared(sim_dir, agents):
            sim_dir.mkdir(parents=True, exist_ok=True)
            (sim_dir / "reddit_profiles.json").write_text(json.dumps([{"user_id": i} for i in range(agents)]))
            rows = "".join(f'{i},agent {i},agent_{i},"persona, {i}",desc\n' for i in range(agents))
            (sim_dir / "twitter_profiles.csv").write_text("user_id,name,username,user_char,description\n" + rows)
            (sim_dir / "simulation_config.json").write_text(
                json.dumps({"agent_configs": [{"agent_id": i} for i in range(agents)]}))

        prepares = []
        client = mock.Mock()
        client.post_multipart.return_value = {"project_id": "p"}
        client.get.return_value = {"status": "completed", "result": {"graph_id": "g"}}

        def post(path, payload, **kw):
            if path == "/api/simulation/prepare":
                prepares.append(payload)
            return {"/api/graph/build": {"task_id": "t"}, "/api/simulation/create": {"simulation_id": "sim_s"},
                    "/api/simulation/prepare": {"task_id": "t2", "expected_entities_count": 4},
                    "/api/simulation/prepare/status": {"status": "completed"}}[path]

        client.post.side_effect = post
        with tempfile.TemporaryDirectory() as tmp:
            sims = Path(tmp, "simulations")
            write_prepared(sims / "sim_s", 9)
            make = lambda: mirofish_api.RunPipeline(client, "mm", runs_dir=Path(tmp, "runs"), log=lambda m: None,
                                                    poll_interval_s=0.01, simulations_dir=sims)
            with self.assertRaisesRegex(mirofish_api.PrepareMismatch, "expected 4, found reddit_profiles.json 9"):
                make().run([], "q", max_rounds=10, until="prepare")
            self.assertNotIn("force_regenerate", prepares[0])

            write_prepared(sims / "sim_s", 4)
            manifest = make().run([], "q", max_rounds=10, until="prepare")
            self.assertTrue(prepares[1]["force_regenerate"])
            self.assertNotIn("prepare_mismatch", manifest)
            self.assertEqual(manifest["stages"]["prepare"]["result"]["agent_files"],
                             {"reddit_profiles.json": 4, "twitter_profiles.csv": 4, "simulation_config.json": 4})

    def test_single_platform_modes_refused(self):
        pipeline = mirofish_api.RunPipeline(mock.Mock(), "x", runs_dir=Path(tempfile.gettempdir()))
        for platform in ("reddit", "twitter"):
            with self.assertRaisesRegex(ValueError, "never report the end of the rounds"):
                pipeline.run([], "q", max_rounds=10, platform=platform)


class RuntimeHookTests(unittest.TestCase):
    def test_netaudit_hook_logs_connects_and_lookups(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "audit.jsonl")
            code = ("import socket\n"
                    f"socket.getaddrinfo('localhost', {port})\n"
                    f"s = socket.create_connection(('127.0.0.1', {port})); s.close()\n")
            env = dict(os.environ, PYTHONPATH=str(OPS_DIR / "runtime"), MIROFISH_NETAUDIT_LOG=str(log))
            subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=30)
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        listener.close()
        events = {(r["event"], r["host"], r["port"]) for r in records}
        self.assertIn(("socket.getaddrinfo", "localhost", port), events)
        self.assertIn(("socket.connect", "127.0.0.1", port), events)

    @unittest.skipUnless(BACKEND_PYTHON.exists(), "backend venv not installed")
    def test_offline_tokenizer_shim(self):
        code = (
            "import sys, types, sitecustomize\n"
            "from huggingface_hub.errors import OfflineModeIsEnabled\n"
            "class Base:\n"
            "    @classmethod\n"
            "    def _patch_mistral_regex(cls, tokenizer, *a, **k):\n"
            "        raise OfflineModeIsEnabled('no network')\n"
            "module = types.SimpleNamespace(PreTrainedTokenizerBase=Base)\n"
            "sitecustomize._patch_tokenizer_module(module)\n"
            "try:\n"
            "    result = Base._patch_mistral_regex('TOKENIZER', 'id')\n"
            "except OfflineModeIsEnabled:\n"
            "    result = 'RAISED'\n"
            "print(result)\n"
        )
        base_env = dict(os.environ, PYTHONPATH=str(OPS_DIR / "runtime"))
        for offline, expected in (("1", "TOKENIZER"), ("0", "RAISED")):
            out = subprocess.run([str(BACKEND_PYTHON), "-c", code], capture_output=True, text=True, timeout=60,
                                 env=dict(base_env, HF_HUB_OFFLINE=offline))
            self.assertEqual(out.stdout.strip(), expected, out.stderr[-500:])


class InjectionCheckTests(unittest.TestCase):
    def test_scan_counts_tokens_case_insensitively(self):
        result = injection_check.scan("posts", [("p1", "hello purple-heron-2208"), ("p2", "nothing"),
                                                ("p3", "ZEBRA-CANARY-7731 and PURPLE-HERON-2208")])
        self.assertEqual(result["items"], 3)
        self.assertEqual(result["hits"]["PURPLE-HERON-2208"]["count"], 2)
        self.assertEqual(result["hits"]["ZEBRA-CANARY-7731"]["examples"][0]["id"], "p3")


if __name__ == "__main__":
    unittest.main()
