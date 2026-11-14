#!/usr/bin/env python3
"""Edit mirofish/.env safely (mode 600, atomic replace, existing values preserved).

  envfile.py sync EXAMPLE TARGET        append keys from EXAMPLE that TARGET lacks
  envfile.py set TARGET KEY=VALUE ...   set or replace keys in TARGET
  envfile.py get TARGET KEY             print a key's value (exit 1 if absent)
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

# python-dotenv also accepts an optional "export " prefix, and the last occurrence of a key wins.
KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def unquote(raw: str) -> str:
    """Value as python-dotenv reads it: quotes removed, or an unquoted ' #' comment dropped."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse(lines: List[str]) -> Dict[str, str]:
    values = {}
    for line in lines:
        match = KEY_LINE.match(line)
        if match:
            values[match.group(1)] = unquote(match.group(2))
    return values


def write_private(path: Path, lines: List[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_lines(path: Path) -> List[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def sync(example: Path, target: Path) -> List[str]:
    """Append example keys missing from target; return the keys added."""
    target_lines = read_lines(target)
    present = parse(target_lines)
    added = []
    for line in read_lines(example):
        match = KEY_LINE.match(line)
        if match and match.group(1) not in present:
            added.append(match.group(1))
            target_lines.append(line)
    if added:
        write_private(target, target_lines)
    return added


def set_values(target: Path, assignments: Dict[str, str]) -> None:
    for key, value in assignments.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value:
            raise ValueError(f"invalid assignment for {key!r}")
    lines = read_lines(target)
    found = set()
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line)
        if match and match.group(1) in assignments:
            key = match.group(1)
            lines[index] = f"{key}={assignments[key]}\n"
            found.add(key)
    lines.extend(f"{key}={value}\n" for key, value in assignments.items() if key not in found)
    write_private(target, lines)


def main(argv: List[str]) -> int:
    if len(argv) >= 3 and argv[0] == "sync":
        added = sync(Path(argv[1]), Path(argv[2]))
        print(f"added {', '.join(added)}" if added else "no missing keys")
        return 0
    if len(argv) >= 3 and argv[0] == "set":
        set_values(Path(argv[1]), dict(arg.split("=", 1) for arg in argv[2:]))
        return 0
    if len(argv) == 3 and argv[0] == "get":
        value = parse(read_lines(Path(argv[1]))).get(argv[2])
        if value is None:
            return 1
        print(value)
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
