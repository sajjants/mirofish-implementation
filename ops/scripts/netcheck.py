#!/usr/bin/env python3
"""Evaluate the network activity of a MiroFish run (used by netcheck.sh).

  netcheck.py sample   --out FILE --pidfile F [--pidfile F ...] [--ollama-port 11434] [--interval 1]
  netcheck.py evaluate --audit FILE --lsof FILE [--allowed ops/config/allowed_hosts.txt]
                       [--expect-simulation] [--report FILE]

`sample` polls lsof for the internet sockets of the given processes, all their
descendants, and whichever process listens on the Ollama port, until SIGTERM.
`evaluate` combines those samples with the Python audit log written by
ops/runtime/sitecustomize.py. It exits 1 if a MiroFish process connected to, sent to
or resolved a host that is neither loopback nor allow-listed, if Ollama (which
receives every prompt) had a non-loopback socket, or if the evidence is missing:
no loopback connection logged from the backend (or, with --expect-simulation, from a
simulation process), or no lsof samples. Missing evidence means the audit did not run,
so a clean result would prove nothing.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
SIMULATION_PROCESS = re.compile(r"^run_\w*simulation\w*\.py$")
LSOF_ENDPOINT = re.compile(r"^(?P<local>.+?)(?:->(?P<remote>.+))?$")


def load_allowlist(path: Optional[Path]) -> Set[str]:
    """Hostnames, IP addresses or CIDR ranges, one per line; '#' starts a comment."""
    if path is None or not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if entry:
            entries.add(entry)
    return entries


def resolve_allowlist(entries: Set[str]) -> List[ipaddress._BaseNetwork]:
    """IP networks covered by the allow-list (hostnames are resolved once, here)."""
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(entry, None):
                networks.append(ipaddress.ip_network(info[4][0]))
        except OSError:
            pass
    return networks


def _strip_brackets(host: str) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.split("%", 1)[0]


def is_loopback(host: Optional[str]) -> bool:
    if host is None:
        return True
    host = _strip_brackets(host).lower()
    if host in LOOPBACK_NAMES or host == "":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def is_allowed(host: Optional[str], names: Set[str], networks: List[ipaddress._BaseNetwork]) -> bool:
    if is_loopback(host):
        return True
    host = _strip_brackets(host or "").lower()
    if host in names:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def evaluate_audit(records: Iterable[Dict], names: Set[str], networks: List) -> List[Dict]:
    """Return audit records that reached or resolved a host outside loopback and the allow-list."""
    violations = []
    for record in records:
        if record.get("family") == "unix":
            continue
        host = record.get("host")
        if record.get("event") in ("socket.getaddrinfo", "socket.gethostbyname"):
            if host and not is_allowed(host, names, networks):
                violations.append(record)
        elif not is_allowed(host, names, networks):
            violations.append(record)
    return violations


def split_host_port(endpoint: str) -> (str, str):
    host, _, port = endpoint.rpartition(":")
    return _strip_brackets(host), port


def parse_lsof_name(name: str) -> Dict[str, Optional[str]]:
    """Parse an lsof NAME such as '127.0.0.1:5001->127.0.0.1:50122' or '*:3000'."""
    name = name.split(" (", 1)[0].strip()
    match = LSOF_ENDPOINT.match(name)
    local, remote = match.group("local"), match.group("remote")
    result = {"local": local, "remote": remote, "remote_host": None}
    if remote:
        result["remote_host"] = split_host_port(remote)[0]
    return result


def evaluate_lsof(samples: Iterable[Dict], names: Set[str], networks: List) -> List[Dict]:
    """Return sockets connected to a disallowed host, or listening on a non-loopback address.
    Ollama sockets may only reach loopback: the allow-list applies to MiroFish alone."""
    violations = []
    seen = set()
    for sample in samples:
        parsed = parse_lsof_name(sample["name"])
        key = (sample.get("group"), sample.get("command"), sample["name"])
        if key in seen:
            continue
        if parsed["remote"]:
            if sample.get("group") == "ollama":
                bad = not is_loopback(parsed["remote_host"])
            else:
                bad = not is_allowed(parsed["remote_host"], names, networks)
        elif sample.get("proto") == "UDP":
            bad = False  # unbound/unconnected UDP; datagrams it sends are caught by the audit hook
        else:
            local_host = split_host_port(parsed["local"])[0]
            bad = local_host == "*" or not is_loopback(local_host)
        if bad:
            seen.add(key)
            violations.append(sample)
    return violations


def read_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def missing_evidence(audit: List[Dict], samples: List[Dict], expect_simulation: bool) -> List[str]:
    def loopback_connects(match) -> int:
        return sum(1 for r in audit if r.get("event") == "socket.connect" and r.get("family") != "unix"
                   and is_loopback(r.get("host")) and match(r.get("proc") or ""))

    missing = []
    if not loopback_connects(lambda proc: proc == "run.py"):
        missing.append("no loopback connection logged from the backend (audit hook not active?)")
    if expect_simulation and not loopback_connects(SIMULATION_PROCESS.match):
        missing.append("no loopback connection logged from a simulation process")
    if not any(s.get("group") == "mirofish" for s in samples):
        missing.append("no lsof samples of MiroFish processes")
    return missing


def evaluate(audit_path: Path, lsof_path: Path, allowed_path: Optional[Path],
             expect_simulation: bool = False) -> Dict:
    names = load_allowlist(allowed_path)
    networks = resolve_allowlist(names) if names else []
    audit = read_jsonl(audit_path)
    samples = read_jsonl(lsof_path)
    audit_violations = evaluate_audit(audit, names, networks)
    lsof_violations = evaluate_lsof(samples, names, networks)
    missing = missing_evidence(audit, samples, expect_simulation)
    destinations = sorted({
        f"{r.get('event')} {r.get('host')}:{r.get('port')}" for r in audit if r.get("family") != "unix"
    })
    return {
        "allowed_hosts": sorted(names),
        "audit_events": len(audit),
        "lsof_samples": len(samples),
        "mirofish_destinations": destinations,
        "violations": {"audit": audit_violations, "lsof": lsof_violations},
        "missing_evidence": missing,
        "passed": not audit_violations and not lsof_violations and not missing,
    }


def descendants(pid: int) -> List[int]:
    found, frontier = [], [pid]
    while frontier:
        current = frontier.pop()
        out = subprocess.run(["pgrep", "-P", str(current)], capture_output=True, text=True).stdout
        children = [int(p) for p in out.split()]
        found.extend(children)
        frontier.extend(children)
    return found


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def lsof_sockets(pids: List[int]) -> List[Dict]:
    if not pids:
        return []
    out = subprocess.run(["lsof", "-nP", "-a", "-i", "-p", ",".join(map(str, pids)), "-F", "pcPn"],
                         capture_output=True, text=True).stdout
    rows, pid, command, proto = [], None, None, None
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value)
        elif tag == "c":
            command = value
        elif tag == "P":
            proto = value
        elif tag == "n":
            rows.append({"pid": pid, "command": command, "proto": proto, "name": value})
    return rows


def listening_pids(port: int) -> List[int]:
    out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                         capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def sample(out: Path, pidfiles: List[Path], ollama_port: Optional[int], interval: float) -> int:
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    with open(out, "a", encoding="utf-8") as handle:
        while not stop["flag"]:
            groups = {"mirofish": [], "ollama": []}
            for pidfile in pidfiles:
                pid = read_pid(pidfile)
                if pid:
                    groups["mirofish"].extend([pid] + descendants(pid))
            if ollama_port:
                for pid in listening_pids(ollama_port):
                    groups["ollama"].extend([pid] + descendants(pid))
            now = time.time()
            for group, pids in groups.items():
                for row in lsof_sockets(pids):
                    row.update(ts=now, group=group)
                    handle.write(json.dumps(row) + "\n")
            handle.flush()
            time.sleep(interval)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--pidfile", action="append", required=True, type=Path)
    s.add_argument("--ollama-port", type=int)
    s.add_argument("--interval", type=float, default=1.0)
    e = sub.add_parser("evaluate")
    e.add_argument("--audit", required=True, type=Path)
    e.add_argument("--lsof", required=True, type=Path)
    e.add_argument("--allowed", type=Path)
    e.add_argument("--report", type=Path)
    e.add_argument("--expect-simulation", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "sample":
        return sample(args.out, args.pidfile, args.ollama_port, args.interval)

    result = evaluate(args.audit, args.lsof, args.allowed, args.expect_simulation)
    if args.report:
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2) + "\n")
    print(f"Python-level network events from MiroFish processes: {result['audit_events']}")
    for destination in result["mirofish_destinations"]:
        print(f"  {destination}")
    print(f"lsof socket samples: {result['lsof_samples']}")
    if result["passed"]:
        print("PASS: no non-loopback connections from MiroFish processes"
              + (f" beyond allowed hosts {result['allowed_hosts']}" if result["allowed_hosts"] else ""))
        return 0
    print("FAIL:")
    for reason in result["missing_evidence"]:
        print(f"  evidence: {reason}")
    for record in result["violations"]["audit"]:
        print(f"  audit: {record.get('proc')} pid {record.get('pid')} {record.get('event')} "
              f"{record.get('host')}:{record.get('port')}")
    for record in result["violations"]["lsof"]:
        print(f"  lsof:  [{record.get('group')}] {record.get('command')} pid {record.get('pid')} "
              f"{record.get('proto')} {record.get('name')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
