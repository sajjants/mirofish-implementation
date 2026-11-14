#!/usr/bin/env python3
"""Turn seed.md + questions.md into the files a batch uploads to MiroFish.

  prepare_inputs.py --batch BATCH_ID --seed seed.md --questions questions.md [--terms terms.txt] [--keep-flagged]
  prepare_inputs.py --batch BATCH_ID --submit Q1 [--max-rounds 10] [--until prepare]

The first form writes ops/runs/<BATCH_ID>/ (0700):
  inputs/seed.original.md   the seed after the instruction lint (stays local; factguard reads it)
  inputs/seed.md            what is uploaded: the linted seed, pseudonymised if --terms is given
  inputs/<Qn>.requirement.txt  the simulation requirement sent with each question
  inputs.json               facts, UNKNOWNs, questions, lint findings and input hashes
With --terms the mapping goes to ops/mappings/<BATCH_ID>.mapping.json (git-ignored, 0600).

The lint drops HTML comments and lines that address a model ('ignore previous instructions',
'SYSTEM NOTE', 'begin the report with ...'). It is a heuristic; see ops/INJECTION_TEST.md.

The second form drives the REST API for one question with the unmodified seed (no UNKNOWN
variant), e.g. to inspect the agents MiroFish would create: --until prepare stops before rounds.
Batches of runs with variants are what run_batch.py is for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seedfile  # noqa: E402
from mirofish_api import MiroFishClient, RunPipeline, sha256_file, write_private_json  # noqa: E402
from opsconfig import MAPPINGS_DIR, RUNS_DIR, make_private_dir, write_private_text  # noqa: E402
from pseudonymize import parse_terms, pseudonymize_texts, suggest  # noqa: E402

BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")


def validate_batch_id(batch_id: str) -> str:
    if not BATCH_ID.fullmatch(batch_id or ""):
        raise ValueError(f"invalid batch id {batch_id!r}: letters, digits and '-' only (max 40)")
    return batch_id


def batch_dir(batch_id: str) -> Path:
    return RUNS_DIR / validate_batch_id(batch_id)


def load_inputs(batch_id: str) -> Dict:
    path = batch_dir(batch_id) / "inputs.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run prepare_inputs.py --batch {batch_id} --seed ... first")
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(batch_id: str, seed_path: Path, questions_path: Path, terms_path: Optional[Path] = None,
            keep_flagged: bool = False, rng_seed: Optional[int] = None) -> Dict:
    target = batch_dir(batch_id)
    if (target / "inputs.json").exists():
        raise FileExistsError(f"{target} already has inputs; choose a new batch id (or purge this one)")
    raw_seed = seed_path.read_text(encoding="utf-8")
    raw_questions = questions_path.read_text(encoding="utf-8")
    seed_text, findings = seedfile.lint_seed(raw_seed, keep_flagged=keep_flagged)
    original_seed = seed_text
    seedfile.parse_questions(raw_questions)  # fail early on a malformed questions file

    mapping_path = None
    leftovers: List[str] = []
    if terms_path:
        terms = parse_terms(terms_path.read_text(encoding="utf-8"))
        out, mapping = pseudonymize_texts({"seed": seed_text, "questions": raw_questions}, terms, rng_seed)
        seed_text, questions_text = out["seed"], out["questions"]
        mapping["inputs"] = [{"name": p.name, "sha256": sha256_file(p)} for p in (seed_path, questions_path, terms_path)]
        mapping_path = make_private_dir(MAPPINGS_DIR) / f"{batch_id}.mapping.json"
        write_private_json(mapping_path, mapping)
        leftovers = suggest(seed_text + "\n" + questions_text, [])
    else:
        questions_text = raw_questions

    facts = seedfile.parse_facts(seed_text)
    questions = seedfile.parse_questions(questions_text)
    if not questions:
        raise ValueError(f"{questions_path}: no '## Q<n>' sections found")

    make_private_dir(RUNS_DIR)
    inputs_dir = make_private_dir(target / "inputs")
    write_private_text(inputs_dir / "seed.original.md", original_seed)
    write_private_text(inputs_dir / "seed.md", seed_text)
    write_private_text(inputs_dir / "questions.md", questions_text)
    question_entries = []
    for question in questions:
        requirement = seedfile.requirement_text(question)
        name = f"{question.id}.requirement.txt"
        write_private_text(inputs_dir / name, requirement)
        question_entries.append({"id": question.id, "title": question.title, "requirement_file": f"inputs/{name}",
                                 "requirement_sha256": hashlib.sha256(requirement.encode()).hexdigest()})
    counts = {tag: sum(1 for f in facts if f.tag == tag) for tag in seedfile.TAGS}
    inputs = {
        "batch_id": batch_id,
        "created_at": time.time(),
        "sources": {"seed": {"name": seed_path.name, "sha256": sha256_file(seed_path)},
                    "questions": {"name": questions_path.name, "sha256": sha256_file(questions_path)}},
        "upload_seed_sha256": hashlib.sha256(seed_text.encode()).hexdigest(),
        "pseudonymized": bool(terms_path),
        "mapping_file": str(mapping_path) if mapping_path else None,
        "lint": {"keep_flagged": keep_flagged, "findings": findings},
        "fact_counts": counts,
        "facts": [f.to_dict() for f in facts],
        "unknowns": [f.to_dict() for f in seedfile.unknowns(facts)],
        "questions": question_entries,
        "unlisted_capitalised_phrases": leftovers[:50],
    }
    write_private_json(target / "inputs.json", inputs)
    return inputs


def summary(inputs: Dict) -> str:
    counts = inputs["fact_counts"]
    lines = [f"Batch {inputs['batch_id']}: {len(inputs['questions'])} question(s); facts "
             + ", ".join(f"{tag} {counts[tag]}" for tag in seedfile.TAGS)]
    for finding in inputs["lint"]["findings"]:
        lines.append(f"  LINT {finding['action']}: line {finding['line']} [{finding['rule']}] {finding['excerpt']}")
    if not sum(counts.values()):
        lines.append("  WARNING: no tagged facts found (lines must start with VERIFIED:, ESTIMATE:, BELIEF: or UNKNOWN:)")
    if inputs["pseudonymized"]:
        lines.append(f"  pseudonymised; mapping in {inputs['mapping_file']} (keep private)")
        if inputs["unlisted_capitalised_phrases"]:
            lines.append("  review capitalised phrases left in the upload: "
                         + ", ".join(inputs["unlisted_capitalised_phrases"][:15]))
    lines.append(f"  next: ops/scripts/unknown_variants.py propose --batch {inputs['batch_id']}"
                 if inputs["unknowns"] else f"  next: ops/scripts/run_batch.py pilot --batch {inputs['batch_id']}")
    return "\n".join(lines)


def submit(batch_id: str, question_id: str, max_rounds: int, until: Optional[str]) -> Dict:
    inputs = load_inputs(batch_id)
    target = batch_dir(batch_id)
    question = next((q for q in inputs["questions"] if q["id"] == question_id), None)
    if question is None:
        raise KeyError(f"no question {question_id} in batch {batch_id}")
    requirement = (target / question["requirement_file"]).read_text(encoding="utf-8")
    run_id = f"{batch_id}_{question_id.lower()}_manual"
    return RunPipeline(MiroFishClient(), run_id).run(
        [target / "inputs" / "seed.md"], requirement, max_rounds=max_rounds, until=until)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--terms", type=Path, help="pseudonymise names listed in this file (see pseudonymize.py)")
    parser.add_argument("--keep-flagged", action="store_true", help="report lint findings but keep those lines")
    parser.add_argument("--submit", metavar="QID")
    parser.add_argument("--max-rounds", type=int, default=10)
    parser.add_argument("--until", choices=("prepare",))
    args = parser.parse_args(argv)

    if args.submit:
        manifest = submit(args.batch, args.submit, args.max_rounds, args.until)
        print(json.dumps({k: manifest.get(k) for k in ("run_id", "project_id", "graph_id", "simulation_id")}, indent=2))
        return 0
    if not (args.seed and args.questions):
        parser.error("--seed and --questions are required (or use --submit)")
    print(summary(prepare(args.batch, args.seed, args.questions, args.terms, args.keep_flagged)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
