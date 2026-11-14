"""Shared paths and config loading for the Phase 4 wrapper scripts.

PyYAML is not in the standard library, but it is pinned in upstream's uv.lock and installed in
mirofish/backend/.venv. ensure_yaml() re-runs the calling script with that interpreter when the
current one lacks it, so no extra dependency is installed anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

OPS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = OPS_DIR.parent
UPSTREAM_DIR = PROJECT_ROOT / "mirofish"
BACKEND_PYTHON = UPSTREAM_DIR / "backend" / ".venv" / "bin" / "python"
RUNS_DIR = OPS_DIR / "runs"
MAPPINGS_DIR = OPS_DIR / "mappings"
STATE_DIR = OPS_DIR / "state"
LOGS_DIR = OPS_DIR / "logs"
METER_LOG = LOGS_DIR / "llm_meter.jsonl"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import parse, read_lines  # noqa: E402


def ensure_yaml() -> None:
    """Import PyYAML, re-executing under the backend venv interpreter if it is missing."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        if BACKEND_PYTHON.exists() and Path(sys.executable).resolve() != BACKEND_PYTHON.resolve():
            os.execv(str(BACKEND_PYTHON), [str(BACKEND_PYTHON), *sys.argv])
        raise SystemExit("PyYAML is not available; run ops/scripts/bootstrap.sh first")


def load_yaml(path: Path) -> Dict[str, Any]:
    import yaml
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data


def model_conf() -> Dict[str, str]:
    """ops/config/model.conf (bash KEY=value syntax, read with the .env parser)."""
    return parse(read_lines(OPS_DIR / "config" / "model.conf"))


def env_values() -> Dict[str, str]:
    return parse(read_lines(UPSTREAM_DIR / ".env"))


def make_private_dir(path: Path) -> Path:
    """Create path and any missing parents as 0700 (mkdir(parents=True) applies the mode to the leaf only)."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private_text(path: Path, text: str) -> None:
    """Atomically write a text file readable only by the current user."""
    import tempfile
    make_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def git_info(repo: Path) -> Optional[Dict[str, Any]]:
    """HEAD commit and dirty flag of a git checkout, or None if git or the repo is missing."""
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": head, "dirty": bool(dirty)}
