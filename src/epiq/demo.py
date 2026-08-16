"""Reproducible example datasets."""

from __future__ import annotations

from .store import Store

PATRIOTS_SOURCE = "https://www.patriots.com/schedule/2025/"
PATRIOTS_GAMES = [
    (1, "2025-09-07", "Las Vegas Raiders", "L", 13, 20),
    (2, "2025-09-14", "at Miami Dolphins", "W", 33, 27),
    (3, "2025-09-21", "Pittsburgh Steelers", "L", 14, 21),
    (4, "2025-09-28", "Carolina Panthers", "W", 42, 13),
    (5, "2025-10-05", "at Buffalo Bills", "W", 23, 20),
    (6, "2025-10-12", "at New Orleans Saints", "W", 25, 19),
    (7, "2025-10-19", "at Tennessee Titans", "W", 31, 13),
    (8, "2025-10-26", "Cleveland Browns", "W", 32, 13),
    (9, "2025-11-02", "Atlanta Falcons", "W", 24, 23),
    (10, "2025-11-09", "at Tampa Bay Buccaneers", "W", 28, 23),
    (11, "2025-11-13", "New York Jets", "W", 27, 14),
    (12, "2025-11-23", "at Cincinnati Bengals", "W", 26, 20),
    (13, "2025-12-01", "New York Giants", "W", 33, 15),
    (15, "2025-12-14", "Buffalo Bills", "L", 31, 35),
    (16, "2025-12-21", "at Baltimore Ravens", "W", 28, 24),
    (17, "2025-12-28", "at New York Jets", "W", 42, 10),
    (18, "2026-01-04", "Miami Dolphins", "W", 38, 10),
]


def load_patriots(store: Store, actor: str = "demo:patriots") -> dict[str, object]:
    """Load the official Patriots 2025 regular-season progression."""
    season_id = store.add_entity("Season", "New England Patriots 2025", {}, actor)
    question_id = store.add_question(
        "game_result",
        "Game",
        "Enum[W,L,T]",
        {
            "prompt": "What was the final result for New England?",
            "sources": ["official_team", "official_league"],
            "cardinality": "one",
            "resolver": "contest_on_conflict",
        },
        actor,
    )
    claims: list[dict[str, str | int]] = []
    for ordinal, (week, date, opponent, result, points_for, points_against) in enumerate(
        PATRIOTS_GAMES, 1
    ):
        game_id = store.add_entity(
            "Game",
            f"Patriots 2025 Week {week}",
            {
                "season_id": season_id,
                "week": week,
                "ordinal": ordinal,
                "date": date,
                "opponent": opponent,
            },
            actor,
        )
        _, evidence_id = store.add_evidence(
            PATRIOTS_SOURCE,
            "Patriots Historical 2025 Schedule",
            "2026-08-15",
            f"Week {week}: New England {result}, {points_for}-{points_against}, {opponent}.",
            actor,
        )
        recorded_at = f"{date}T23:59:59Z"
        claim_id = store.assert_claim(
            game_id, question_id, result, date, evidence_id, actor, recorded_at=recorded_at
        )
        claims.append({"week": week, "claim_id": claim_id, "recorded_at": recorded_at})
    return {
        "season_id": season_id,
        "question_id": question_id,
        "games": len(PATRIOTS_GAMES),
        "claims": claims,
        "final": store.season_record(season_id),
    }
