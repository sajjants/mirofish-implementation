#!/usr/bin/env python3
"""Restore original names and figures in MiroFish outputs produced from a pseudonymised seed.

  depseudonymize.py --mapping ops/mappings/NAME.mapping.json FILE [FILE ...] [--out-dir DIR | --stdout]

Pseudonyms and pseudonymised figures that appear verbatim are replaced with the originals.
Figures the model derived itself (sums, rounded values) cannot be mapped back exactly; any
remaining currency amounts are listed so a reader knows they are on the pseudonymised scale
(divide by the mapping's scale_factor). Output files are written 0600 as <name>.depseudo<ext>.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from opsconfig import write_private_text  # noqa: E402
from pseudonymize import NUMBER  # noqa: E402


def load_mapping(path: Path) -> Dict:
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if mapping.get("version") != 1:
        raise ValueError(f"{path}: unsupported mapping version {mapping.get('version')!r}")
    return mapping


def reverse_table(mapping: Dict) -> Dict[str, str]:
    """pseudonym -> original. The first original listed for a pseudonym (the canonical name) wins."""
    table: Dict[str, str] = {}
    for entry in mapping.get("names", []):
        table.setdefault(entry["pseudonym"], entry["original"])
    for entry in mapping.get("figures", []):
        table.setdefault(entry["pseudonym"], entry["original"])
    return table


def depseudonymize(text: str, mapping: Dict) -> Tuple[str, List[str]]:
    """Return (restored_text, unmapped_currency_amounts)."""
    table = reverse_table(mapping)
    if table:
        alternatives = sorted(table, key=len, reverse=True)
        # Not inside a longer word or number ('451' must not match within '1,451' or '451.5').
        pattern = re.compile(r"(?<![\w.,])(" + "|".join(re.escape(a) for a in alternatives) + r")(?![\w]|[.,]\d)")
        restored = pattern.sub(lambda m: table[m.group(1)], text)
    else:
        restored = text
    originals = {e["original"] for e in mapping.get("figures", [])}
    unmapped = []
    for match in NUMBER.finditer(restored):
        is_money = match["cur"] or match["scale"] or re.search(r"dollars|euros|pounds", match["unit"] or "")
        if is_money and match.group(0) not in originals:
            unmapped.append(match.group(0).strip())
    return restored, sorted(set(unmapped))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--mapping", required=True, type=Path)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--out-dir", type=Path)
    target.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    mapping = load_mapping(args.mapping)
    for path in args.files:
        restored, unmapped = depseudonymize(path.read_text(encoding="utf-8"), mapping)
        if args.stdout:
            sys.stdout.write(restored)
        else:
            out = (args.out_dir or path.parent) / f"{path.stem}.depseudo{path.suffix}"
            write_private_text(out, restored)
            print(f"wrote {out}")
        if unmapped:
            print(f"{path.name}: amounts still on the pseudonymised scale (divide by "
                  f"{mapping['scale_factor']}): {', '.join(unmapped)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
