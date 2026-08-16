"""A deliberately narrow, statically checked EpiQL parser."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import EpiqError

QUESTION = re.compile(
    r"question\s+(?P<name>[a-z_][a-z0-9_]*)\s*:\s*(?P<type>[^\n]+?)\s+for\s+"
    r"(?P<subject>[A-Za-z][A-Za-z0-9_]*)\s*\{(?P<body>.*?)\}",
    re.DOTALL,
)
DERIVE = re.compile(
    r"derive\s+(?P<name>[a-z_][a-z0-9_]*)\s*:\s*(?P<type>[A-Za-z][A-Za-z0-9_\[\], ]*)"
    r"\s+for\s+(?P<subject>[A-Za-z][A-Za-z0-9_]*)\s*=\s*(?P<body>.*?)"
    r"(?=\n\s*(?:derive|view)\b|\s*\Z)",
    re.DOTALL,
)
COUNT_BODY = re.compile(
    r"(?P<relation>[a-z_][a-z0-9_]*)\s*\|>\s*where\s+"
    r"(?P<question>[a-z_][a-z0-9_]*)\s*==\s*(?P<value>[A-Za-z0-9_]+)\s*\|>\s*count\s*$"
)


@dataclass(frozen=True)
class Question:
    """A parsed question declaration."""

    name: str
    value_type: str
    subject_kind: str
    body: str


@dataclass(frozen=True)
class CountDerivation:
    """A parsed count-over-filter derivation."""

    name: str
    value_type: str
    subject_kind: str
    relation: str
    question: str
    value: str


@dataclass(frozen=True)
class Program:
    """The statically checked portion of an EpiQL program."""

    questions: tuple[Question, ...]
    derivations: tuple[CountDerivation, ...]


def parse(source: str) -> Program:
    """Parse supported EpiQL declarations and reject unsupported derivations."""
    questions = tuple(
        Question(
            name=match.group("name"),
            value_type=match.group("type").strip(),
            subject_kind=match.group("subject"),
            body=match.group("body").strip(),
        )
        for match in QUESTION.finditer(source)
    )
    derivations: list[CountDerivation] = []
    for match in DERIVE.finditer(source):
        body = " ".join(match.group("body").split())
        count = COUNT_BODY.fullmatch(body)
        if not count:
            raise EpiqError(
                "unsupported_derivation",
                "EpiQL v0.1 only supports relation |> where question == value |> count; "
                f"got {body!r}",
            )
        derivations.append(
            CountDerivation(
                name=match.group("name"),
                value_type=match.group("type").strip(),
                subject_kind=match.group("subject"),
                relation=count.group("relation"),
                question=count.group("question"),
                value=count.group("value"),
            )
        )
    if not questions and not derivations:
        raise EpiqError("empty_program", "No supported question or derive declarations found")
    names = {question.name for question in questions}
    missing = sorted({derive.question for derive in derivations} - names)
    if missing:
        raise EpiqError(
            "unknown_question", f"Derivations reference undefined questions: {', '.join(missing)}"
        )
    return Program(questions=questions, derivations=tuple(derivations))


def describe(program: Program) -> dict[str, object]:
    """Return a JSON-safe intermediate representation."""
    return {
        "effect": "pure",
        "questions": [question.__dict__ for question in program.questions],
        "derivations": [derive.__dict__ for derive in program.derivations],
    }
