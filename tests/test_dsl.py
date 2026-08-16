from pathlib import Path

import pytest

from epiq.dsl import parse
from epiq.errors import EpiqError


def test_patriots_program_parses() -> None:
    program = parse(Path("examples/patriots.epiq").read_text())
    assert [question.name for question in program.questions] == ["game_result"]
    assert [(derive.name, derive.value) for derive in program.derivations] == [
        ("wins", "W"),
        ("losses", "L"),
    ]


def test_undefined_question_is_rejected() -> None:
    with pytest.raises(EpiqError, match="undefined questions"):
        parse("derive wins : Int for Season = games |> where result == W |> count")


def test_unsupported_derivation_is_rejected() -> None:
    source = """
question result : Enum[W,L] for Game { cardinality one }
derive wins : Int for Season = games |> sum points
"""
    with pytest.raises(EpiqError, match="only supports"):
        parse(source)
