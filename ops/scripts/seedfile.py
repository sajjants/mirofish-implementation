"""Parse seed.md / questions.md, lint seeds for embedded instructions, apply UNKNOWN variants.

Seed facts are lines tagged VERIFIED, ESTIMATE, BELIEF or UNKNOWN, optionally bulleted and
optionally with an explicit id:   - UNKNOWN [U-strike]: Whether the union will strike.
Questions are '## Q<n>' sections:  ## Q1: Will the council pass Proposal 14?
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

TAGS = ("VERIFIED", "ESTIMATE", "BELIEF", "UNKNOWN")
FACT_LINE = re.compile(
    r"^(?P<prefix>\s*(?:[-*+]\s+)?)(?P<tag>VERIFIED|ESTIMATE|BELIEF|UNKNOWN)"
    r"(?:\s*\[(?P<id>[A-Za-z0-9_-]{1,32})\])?\s*:\s*(?P<text>\S.*?)\s*$"
)
QUESTION_HEADING = re.compile(r"^##\s+(?P<id>Q\d+)\b\s*[:.\-]?\s*(?P<title>.*?)\s*$")
HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")

# Phrases that address the model rather than describe the world. A heuristic: it catches the
# common shapes seen in INJECTION_TEST.md, not every possible injection.
INJECTION_RULES: List[Tuple[str, re.Pattern]] = [
    ("ignore-instructions", re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)"
        r"(?:\s+\w+)?\s+(?:instructions?|prompts?|rules)\b", re.I)),
    ("role-override", re.compile(r"\byou\s+are\s+now\b|\bmaintenance\s+mode\b|\bdeveloper\s+mode\b", re.I)),
    ("system-message", re.compile(r"\b(?:system|developer)\s+(?:note|prompt|message|instruction)s?\b", re.I)),
    ("chat-role-prefix", re.compile(r"^\s*>?\s*(?:system|assistant|developer)\s*:", re.I)),
    ("pipeline-instruction", re.compile(
        r"\b(?:report\s+writer|ontology\s+instruction|persona\s+instruction)\b|\badd\s+an?\s+entity\s+type\b", re.I)),
    ("output-directive", re.compile(
        r"\b(?:begin|start)\s+(?:the|your)\s+(?:final\s+)?(?:report|answer|response|output)\s+with\b"
        r"|\b(?:every|each)\s+(?:persona|post|agent|message|reply|response)\s+you\b", re.I)),
]


@dataclass
class Fact:
    id: str
    tag: str
    text: str
    line: int  # 0-based line index in the (linted) seed text

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Question:
    id: str
    title: str
    body: str

    @property
    def text(self) -> str:
        return "\n\n".join(part for part in (self.title, self.body) if part).strip()


def parse_facts(text: str) -> List[Fact]:
    facts: List[Fact] = []
    counters = {tag: 0 for tag in TAGS}
    seen = set()
    for index, line in enumerate(text.splitlines()):
        match = FACT_LINE.match(line)
        if not match:
            continue
        tag = match["tag"]
        counters[tag] += 1
        fact_id = match["id"] or f"{'U' if tag == 'UNKNOWN' else tag[0]}{counters[tag]}"
        if fact_id in seen:
            raise ValueError(f"duplicate fact id {fact_id!r} on line {index + 1}")
        seen.add(fact_id)
        facts.append(Fact(fact_id, tag, match["text"], index))
    return facts


def unknowns(facts: List[Fact]) -> List[Fact]:
    return [f for f in facts if f.tag == "UNKNOWN"]


def parse_questions(text: str) -> List[Question]:
    questions: List[Question] = []
    current: Optional[Question] = None
    body: List[str] = []
    for line in text.splitlines():
        heading = QUESTION_HEADING.match(line)
        if heading or line.startswith("## ") or line.startswith("# "):
            if current:
                current.body = "\n".join(body).strip()
                questions.append(current)
            current, body = None, []
            if heading:
                current = Question(heading["id"], heading["title"], "")
            continue
        if current is not None:
            body.append(line)
    if current:
        current.body = "\n".join(body).strip()
        questions.append(current)
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate question ids in questions file: {ids}")
    empty = [q.id for q in questions if not q.text]
    if empty:
        raise ValueError(f"questions without text: {empty}")
    return questions


def lint_seed(text: str, keep_flagged: bool = False) -> Tuple[str, List[Dict]]:
    """Remove HTML comments and lines that read as instructions to a model.

    Returns (clean_text, findings). With keep_flagged the text is returned unchanged
    apart from HTML comments, which are never useful content for a simulation.
    """
    findings: List[Dict] = []
    for match in HTML_COMMENT.finditer(text):
        findings.append({"rule": "html-comment", "line": text.count("\n", 0, match.start()) + 1,
                         "excerpt": _excerpt(match.group(0)), "action": "removed"})
    text = HTML_COMMENT.sub("", text)
    kept: List[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        rules = [name for name, pattern in INJECTION_RULES if pattern.search(line)]
        if rules:
            findings.append({"rule": ",".join(rules), "line": number, "excerpt": _excerpt(line),
                             "action": "kept" if keep_flagged else "removed"})
            if not keep_flagged:
                continue
        kept.append(line)
    clean = "\n".join(kept)
    return (clean + "\n") if text.endswith("\n") else clean, findings


def _excerpt(value: str, limit: int = 80) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def apply_variant(text: str, facts: List[Fact], choices: Dict[str, str]) -> str:
    """Replace each UNKNOWN line with '<prefix>SCENARIO ASSUMPTION [id]: <chosen alternative>'."""
    lines = text.splitlines()
    for fact in unknowns(facts):
        if fact.id not in choices:
            raise KeyError(f"variant has no value for {fact.id}")
        match = FACT_LINE.match(lines[fact.line])
        if not match or match["tag"] != "UNKNOWN":
            raise ValueError(f"line {fact.line + 1} no longer holds UNKNOWN {fact.id}")
        lines[fact.line] = f"{match['prefix']}SCENARIO ASSUMPTION [{fact.id}]: {choices[fact.id]}"
    result = "\n".join(lines)
    return (result + "\n") if text.endswith("\n") else result


READING_GUIDE = (
    "How to read the seed: statements tagged VERIFIED are established facts; ESTIMATE statements "
    "are approximate figures; BELIEF statements are views held by some participants, not facts; "
    "SCENARIO ASSUMPTION statements are fixed premises for this simulation run; UNKNOWN statements "
    "are open questions that nobody in the scenario can answer yet. Do not invent figures, dates, "
    "people or organisations that are not in the seed."
)


def requirement_text(question: Question) -> str:
    return f"{question.text}\n\n{READING_GUIDE}"
