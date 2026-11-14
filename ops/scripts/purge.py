#!/usr/bin/env python3
"""Securely delete everything a MiroFish run left behind (see purge.sh for usage).

For the run recorded in ops/runs/<RUN_ID>/manifest.json this removes:
  - the project directory (uploaded seed files, extracted text, ontology),
  - the simulation directory (personas, config, posts databases, action logs, IPC files),
  - the report directory (report sections, agent and console logs),
  - the graph in the local memory store (SQLite with secure_delete, so freed pages are zeroed),
  - ops/runs/<RUN_ID>/ (manifest, report copy, netcheck evidence),
  - with --logs, all backend and ops log files (they are not separable per run).
Files are overwritten with zeros and fsynced before being unlinked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mirofish_api import ApiError, MiroFishClient, validate_run_id  # noqa: E402

OPS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = OPS_DIR.parent
BACKEND = PROJECT_ROOT / "mirofish" / "backend"
UPLOADS = BACKEND / "uploads"
UPSTREAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ACTIVE_RUNNER_STATES = {"starting", "running", "paused", "stopping"}
ACTIVE_TASK_STATES = {"pending", "processing"}


def checked_child(base: Path, name: Optional[str]) -> Optional[Path]:
    """base/name if name is a safe single path component that stays inside base, else None."""
    if not name:
        return None
    if not UPSTREAM_ID.fullmatch(name):
        raise ValueError(f"refusing unexpected id {name!r}")
    candidate = base / name
    if candidate.is_symlink():
        raise ValueError(f"refusing to follow symlink {candidate}")
    if candidate.exists() and candidate.resolve().parent != base.resolve():
        raise ValueError(f"{candidate} resolves outside {base}")
    return candidate


def shred_file(path: Path, warnings: Optional[List[str]] = None) -> None:
    """Overwrite a regular file with zeros, fsync, then unlink.

    Symlinks and special files are only unlinked (never opened through). A file with other
    hard links is unlinked without overwriting, because zeroing it would also change the
    other names, which may live outside the tree being purged.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        path.unlink(missing_ok=True)
        return
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            block = b"\0" * (1 << 20)
            remaining = info.st_size
            while remaining > 0:
                remaining -= handle.write(block[:min(remaining, len(block))])
            os.fsync(fd)
        elif stat.S_ISREG(info.st_mode) and warnings is not None:
            warnings.append(f"{path}: has {info.st_nlink} hard links; unlinked without overwriting")
    path.unlink()


def shred_tree(root: Path, warnings: Optional[List[str]] = None) -> int:
    """Shred every file under root (without following symlinks) and remove the tree. Returns file count."""
    if root.is_symlink() or root.is_file():
        shred_file(root, warnings)
        return 1
    if not root.exists():
        return 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            shred_file(Path(dirpath) / name, warnings)
            count += 1
        for name in dirnames:
            entry = Path(dirpath) / name
            if entry.is_symlink():
                entry.unlink()
            else:
                entry.rmdir()
    root.rmdir()
    return count


def delete_graph_offline(graph_id: str) -> str:
    """Delete a graph from the local store with the backend stopped, using the patched store code."""
    code = (
        "import sys\n"
        "from app.local_graph import default_db_path\n"
        "from app.local_graph.store import GraphStore\n"
        "import os\n"
        "path = default_db_path()\n"
        "if not os.path.exists(path):\n"
        "    print('no local graph database'); sys.exit(0)\n"
        "store = GraphStore(path)\n"
        "try:\n"
        "    store.delete_graph(sys.argv[1]); print('deleted')\n"
        "finally:\n"
        "    store.close()\n"
    )
    env = dict(os.environ)
    result = subprocess.run([str(BACKEND / ".venv" / "bin" / "python"), "-c", code, graph_id],
                            cwd=BACKEND, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"graph delete failed: {result.stderr.strip()[-500:]}")
    return result.stdout.strip()


def read_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def discover(run_id: str, manifest: Dict) -> Dict[str, Set[str]]:
    """Ids belonging to the run: those in the manifest, plus what a failed or interrupted run
    left on disk without recording (projects named after the run, the graph id a project
    stores, reports whose metadata names the run's simulation)."""
    found = {kind: {manifest[key]} if manifest.get(key) else set()
             for kind, key in (("projects", "project_id"), ("simulations", "simulation_id"),
                               ("reports", "report_id"), ("graphs", "graph_id"))}
    projects_dir = UPLOADS / "projects"
    if projects_dir.is_dir():
        for entry in projects_dir.iterdir():
            if read_json(entry / "project.json").get("name") == run_id:
                found["projects"].add(entry.name)
    for project_id in found["projects"]:
        graph_id = read_json(projects_dir / project_id / "project.json").get("graph_id")
        if graph_id:
            found["graphs"].add(graph_id)
    reports_dir = UPLOADS / "reports"
    if reports_dir.is_dir() and found["simulations"]:
        for entry in reports_dir.iterdir():
            if read_json(entry / "meta.json").get("simulation_id") in found["simulations"]:
                found["reports"].add(entry.name)
    for graph_id in found["graphs"]:
        if not UPSTREAM_ID.fullmatch(graph_id):
            raise ValueError(f"refusing unexpected graph id {graph_id!r}")
    return found


def filevault_status() -> str:
    try:
        return subprocess.run(["fdesetup", "status"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown (fdesetup not available)"


def purge(run_id: str, *, include_logs: bool, client: Optional[MiroFishClient] = None,
          log=print) -> List[str]:
    run_dir = checked_child(OPS_DIR / "runs", validate_run_id(run_id))
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = discover(run_id, manifest)
    directories = [(kind[:-1], checked_child(UPLOADS / kind, item))
                   for kind in ("projects", "simulations", "reports") for item in sorted(ids[kind])]

    client = client or MiroFishClient()
    running = client.health()
    if running:
        for simulation_id in ids["simulations"]:
            try:
                state = client.get(f"/api/simulation/{simulation_id}/run-status")
            except ApiError:
                state = {}
            if state.get("runner_status") in ACTIVE_RUNNER_STATES:
                raise RuntimeError("the simulation is still running; stop it (or ops/scripts/stop.sh) first")
        try:
            tasks = client.get("/api/graph/tasks")
        except ApiError:
            tasks = []
        if any(t.get("status") in ACTIVE_TASK_STATES for t in tasks if isinstance(t, dict)):
            raise RuntimeError("MiroFish still has graph/prepare/report tasks in progress; wait or stop it first")
    if running and include_logs:
        raise RuntimeError("--logs needs MiroFish stopped (ops/scripts/stop.sh) so log files are not in use")

    done, warnings = [], []
    # Graphs first: if a delete fails, nothing else is touched and the manifest survives for a retry.
    for graph_id in sorted(ids["graphs"]):
        if running:
            try:
                client.delete(f"/api/graph/delete/{graph_id}")
                done.append(f"graph {graph_id}: deleted via API")
            except ApiError as exc:
                if "HTTP 404" not in str(exc):
                    raise RuntimeError(f"graph {graph_id} could not be deleted: {exc}") from None
                done.append(f"graph {graph_id}: already gone")
        else:
            done.append(f"graph {graph_id}: {delete_graph_offline(graph_id)}")
    for label, directory in directories:
        if directory is not None:
            done.append(f"{label} {directory.name}: shredded {shred_tree(directory, warnings)} files")
    if include_logs:
        for log_dir in (BACKEND / "logs", OPS_DIR / "logs"):
            if log_dir.exists():
                count = sum(shred_tree(entry, warnings) for entry in sorted(log_dir.iterdir()))
                done.append(f"logs {log_dir.relative_to(PROJECT_ROOT)}: shredded {count} files")
    done.append(f"run {run_id}: shredded {shred_tree(run_dir, warnings)} files")
    done.extend(f"WARN: {w}" for w in warnings)
    for line in done:
        log(line)
    return done


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id")
    parser.add_argument("--logs", action="store_true", help="also shred all backend and ops logs")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    validate_run_id(args.run_id)
    if not args.yes:
        scope = " and ALL log files" if args.logs else ""
        answer = input(f"Permanently delete run {args.run_id}{scope}? Type the run id to confirm: ")
        if answer.strip() != args.run_id:
            print("aborted")
            return 1
    try:
        purge(args.run_id, include_logs=args.logs)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"FileVault: {filevault_status()}")
    print("Note: on SSDs and APFS, overwriting cannot guarantee the old blocks are physically erased;"
          " full-disk encryption (FileVault) is what protects deleted data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
