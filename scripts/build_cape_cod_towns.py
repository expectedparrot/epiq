"""Build the reproducible Cape Cod towns research database."""

from __future__ import annotations

import argparse
from pathlib import Path

from epiq.store import Store

SOURCE_URL = (
    "https://api.censusreporter.org/1.0/data/show/latest?"
    "table_ids=B01003%2CB25077&geo_ids=060%7C05000US25001"
)
RELEASE = "ACS 2024 5-year (2020–2024)"

# name, county-subdivision GEOID, population, population MOE, home value, home-value MOE
TOWNS = [
    ("Barnstable", "06000US2500103690", 49_568, 34, 602_500, 14_266),
    ("Bourne", "06000US2500107175", 20_323, 24, 575_600, 27_939),
    ("Brewster", "06000US2500107980", 10_420, 19, 707_700, 50_846),
    ("Chatham", "06000US2500112995", 6_681, 19, 942_800, 77_505),
    ("Dennis", "06000US2500116775", 14_868, 24, 627_100, 28_306),
    ("Eastham", "06000US2500119295", 5_811, 17, 732_200, 62_613),
    ("Falmouth", "06000US2500123105", 33_039, 33, 629_900, 19_504),
    ("Harwich", "06000US2500129020", 13_598, 18, 658_600, 25_872),
    ("Mashpee", "06000US2500139100", 15_384, 29, 562_200, 28_913),
    ("Orleans", "06000US2500151440", 6_415, 16, 957_000, 85_172),
    ("Provincetown", "06000US2500155500", 3_703, 33, 902_200, 179_748),
    ("Sandwich", "06000US2500159735", 20_522, 41, 601_400, 25_932),
    ("Truro", "06000US2500170605", 1_708, 322, 888_200, 116_715),
    ("Wellfleet", "06000US2500174385", 4_404, 321, 855_300, 70_470),
    ("Yarmouth", "06000US2500182525", 25_224, 30, 551_800, 25_021),
]


def build(path: str | Path, actor: str = "agent:cape-cod-research") -> dict[str, int]:
    """Create a new two-question Cape Cod town database."""
    store = Store(path)
    store.initialize("Cape Cod Towns: Population and Home Values")
    store.add_question(
        "population",
        "Town",
        "Int",
        {
            "label": "Population estimate",
            "cardinality": "one",
            "unit": "people",
            "release": RELEASE,
        },
        actor,
    )
    store.add_question(
        "median_home_value",
        "Town",
        "Int",
        {
            "label": "Median owner-occupied home value",
            "cardinality": "one",
            "unit": "USD",
            "release": RELEASE,
        },
        actor,
    )

    for name, geoid, population, population_moe, home_value, home_value_moe in TOWNS:
        town_id = store.add_entity(
            "Town",
            name,
            {
                "county": "Barnstable County",
                "state": "Massachusetts",
                "geoid": geoid,
                "region": "Cape Cod",
            },
            actor,
        )
        excerpt = (
            f"{RELEASE} reports {name}'s total population as {population:,} "
            f"(margin of error ±{population_moe:,}) and median value of owner-occupied housing "
            f"units as ${home_value:,} (margin of error ±${home_value_moe:,})."
        )
        _, evidence_id = store.add_evidence(
            SOURCE_URL,
            f"Census Reporter: {name}, Massachusetts",
            "2026-08-15",
            excerpt,
            actor,
        )
        store.assert_claim(town_id, "population", population, "2024-12-31", evidence_id, actor)
        store.assert_claim(
            town_id, "median_home_value", home_value, "2024-12-31", evidence_id, actor
        )

    return {"towns": len(TOWNS), "questions": 2, "claims": len(TOWNS) * 2}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--actor", default="agent:cape-cod-research")
    args = parser.parse_args()
    print(build(args.db, args.actor))


if __name__ == "__main__":
    main()
