"""Review-first schema plans for common evidence-collection projects."""

from __future__ import annotations

import json
from typing import Any

from .errors import EpiqError


def _q(kind: str, name: str, value_type: str, cardinality: str, temporal: str) -> dict[str, str]:
    return {
        "kind": kind,
        "name": name,
        "value_type": value_type,
        "cardinality": cardinality,
        "temporal": temporal,
    }


SCHEMA_DESIGN_CHECKLIST = [
    {
        "step": "scope",
        "questions": [
            "What inclusion and exclusion rules define membership in the collection?",
            "What date or version makes the collection complete enough to evaluate?",
        ],
    },
    {
        "step": "entities",
        "questions": [
            "Which things need stable identity and their own evidence-backed history?",
            "Which repeated events should be observation entities rather than overwritten fields?",
        ],
    },
    {
        "step": "identity",
        "questions": [
            "Which durable identifiers prevent duplicates?",
            "What compound fallback is used when the preferred identifier is unavailable?",
        ],
    },
    {
        "step": "questions",
        "questions": [
            "What value type, cardinality, and temporal policy does each field require?",
            "Should a relationship use Ref[Kind] rather than a string?",
        ],
    },
    {
        "step": "evidence",
        "questions": [
            "Which sources are acceptable for each field?",
            "What excerpt and retrieval date would let another reviewer verify the value?",
        ],
    },
    {
        "step": "pilot",
        "questions": [
            "Do typical, missing-data, ambiguous, duplicate, and changed-over-time cases fit?",
            "Which categories should remain open text until evidence supports a stable taxonomy?",
        ],
    },
]


_PATTERNS: dict[str, dict[str, Any]] = {
    "faculty": {
        "title": "Faculty in an academic department",
        "objective": "Build an evidence-backed roster and research profile for in-scope faculty.",
        "inclusion_rule": (
            "Include people listed as current faculty by the target department at the collection "
            "cutoff; record affiliated, emeritus, visiting, and joint appointments explicitly."
        ),
        "kinds": ["Institution", "Department", "Faculty"],
        "identity": {
            "Institution": ["ror_id", "official_domain"],
            "Department": ["institution_id", "official_department_url"],
            "Faculty": ["orcid", "fallback: department_id + official_profile_url"],
        },
        "questions": [
            _q("Department", "institution", "Ref[Institution]", "one", "stable"),
            _q("Department", "official_url", "URL", "one", "stable"),
            _q("Faculty", "department", "Ref[Department]", "many", "dynamic"),
            _q("Faculty", "appointment_title", "String", "many", "dynamic"),
            _q(
                "Faculty",
                "appointment_status",
                "Enum[current,emeritus,visiting,affiliate,joint,unknown]",
                "one",
                "dynamic",
            ),
            _q("Faculty", "official_profile_url", "URL", "one", "stable"),
            _q("Faculty", "orcid", "String", "one", "stable"),
            _q("Faculty", "research_interests", "Json", "many", "dynamic"),
        ],
        "evidence_policy": [
            "Roster membership and title should cite an official department or institution page.",
            "Research interests may cite official profiles, ORCID, or recent publication records.",
            "Do not infer employment status solely from publication affiliation.",
        ],
        "pilot_cases": [
            "ordinary current faculty member",
            "joint appointment in two departments",
            "emeritus member still shown on the roster",
            "person without ORCID",
            "duplicate name shared by two people",
        ],
    },
    "literature": {
        "title": "Papers on a research topic",
        "objective": (
            "Create a reviewable literature corpus with explicit topic-inclusion evidence."
        ),
        "inclusion_rule": (
            "Include works meeting a written topical and document-type rule; preserve uncertain "
            "relevance for review rather than silently excluding it."
        ),
        "kinds": ["Paper", "Author", "Venue", "ResearchTopic"],
        "identity": {
            "Paper": ["doi", "fallback: normalized title + year + first_author_id"],
            "Author": ["orcid", "fallback: normalized name + affiliation"],
            "Venue": ["issn_or_registry_id", "official_name"],
            "ResearchTopic": ["project_defined_topic_code"],
        },
        "questions": [
            _q("Paper", "doi", "String", "one", "stable"),
            _q("Paper", "publication_date", "Date", "one", "stable"),
            _q("Paper", "authors", "Ref[Author]", "many", "stable"),
            _q("Paper", "venue", "Ref[Venue]", "one", "stable"),
            _q("Paper", "topics", "Ref[ResearchTopic]", "many", "stable"),
            _q("Paper", "abstract", "String", "one", "stable"),
            _q("Paper", "relevance", "Probability", "one", "stable"),
            _q("Paper", "study_design", "String", "one", "stable"),
        ],
        "evidence_policy": [
            "Bibliographic facts should cite DOI/Crossref, publisher, or repository metadata.",
            "Topic relevance must cite title, abstract, keywords, or full-text passage.",
            "Keep search-query provenance and record unsuccessful retrieval separately.",
        ],
        "pilot_cases": [
            "canonical DOI article",
            "preprint later published under a DOI",
            "paper with ambiguous topical relevance",
            "same title used by distinct papers",
            "paper with consortium authorship",
        ],
    },
    "startups": {
        "title": "Startups in a niche market",
        "objective": "Track in-scope companies, positioning, people, and funding over time.",
        "inclusion_rule": (
            "Include companies whose product evidence satisfies a written niche definition at a "
            "specified date; keep adjacent and uncertain companies reviewable."
        ),
        "kinds": ["Company", "Founder", "Investor", "MarketSegment", "FundingRound"],
        "identity": {
            "Company": ["official_domain", "fallback: jurisdiction + legal_name"],
            "Founder": ["stable_profile_url", "fallback: normalized name + company_id"],
            "Investor": ["official_domain", "official_name"],
            "MarketSegment": ["project_defined_segment_code"],
            "FundingRound": ["company_id", "announced_date", "round_type"],
        },
        "questions": [
            _q("Company", "official_domain", "URL", "one", "stable"),
            _q("Company", "founded_year", "Year", "one", "stable"),
            _q(
                "Company",
                "operating_status",
                "Enum[active,acquired,closed,unknown]",
                "one",
                "dynamic",
            ),
            _q("Company", "segments", "Ref[MarketSegment]", "many", "dynamic"),
            _q("Company", "founders", "Ref[Founder]", "many", "stable"),
            _q("Company", "product_summary", "String", "one", "dynamic"),
            _q("FundingRound", "company", "Ref[Company]", "one", "stable"),
            _q("FundingRound", "announced_date", "Date", "one", "stable"),
            _q("FundingRound", "round_type", "String", "one", "stable"),
            _q("FundingRound", "amount", "Quantity[USD]", "one", "stable"),
            _q("FundingRound", "investors", "Ref[Investor]", "many", "stable"),
        ],
        "evidence_policy": [
            "Niche inclusion should cite product language, not a directory category alone.",
            "Funding should cite company/investor announcements or reputable reporting.",
            "Model each funding round separately; never overwrite total funding with one round.",
        ],
        "pilot_cases": [
            "clear pure-play company",
            "adjacent company with one in-scope product",
            "rebranded company with an old domain",
            "acquired or closed company",
            "round reported with conflicting amounts",
        ],
    },
}


def pattern_summaries() -> list[dict[str, str]]:
    return [
        {"pattern": name, "title": value["title"], "objective": value["objective"]}
        for name, value in _PATTERNS.items()
    ]


def schema_plan(pattern: str) -> dict[str, Any]:
    """Return a non-mutating, reviewable schema plan and CLI argv templates."""
    if pattern not in _PATTERNS:
        raise EpiqError(
            "schema_pattern_not_found",
            f"Unknown schema pattern: {pattern}",
            f"Available patterns: {', '.join(sorted(_PATTERNS))}",
        )
    plan = _PATTERNS[pattern]
    return {
        "schema_version": "1.0",
        "pattern": pattern,
        **plan,
        "commands": {
            "kinds": [["epiq", "kind", kind] for kind in plan["kinds"]],
            "questions": [_question_command(item) for item in plan["questions"]],
        },
        "review_before_apply": True,
        "recommended_process": [
            "Write project-specific inclusion and exclusion rules.",
            "Review identities and relationship cardinalities with domain experts.",
            "Create a five-case pilot including ambiguity, duplication, and missing data.",
            "Challenge or evolve questions that fail the pilot; do not force bad values.",
            "Only then scale entity discovery and evidence collection.",
        ],
        "anti_patterns": [
            "comma-separated names where Ref[Kind] relationships are needed",
            "names as the sole identity when durable IDs or compound identities exist",
            "overwriting repeated events such as funding rounds or appointments",
            "definitive Boolean classifications for uncertain inclusion",
            "claims without bounded evidence and retrieval dates",
        ],
    }


def _question_command(question: dict[str, str]) -> list[str]:
    definition = json.dumps(
        {
            "cardinality": question["cardinality"],
            "temporal_policy": question["temporal"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        "epiq",
        "question",
        question["name"],
        "--for",
        question["kind"],
        "--type",
        question["value_type"],
        "--definition",
        definition,
        "--idempotency-key",
        f"schema:{question['kind']}:{question['name']}",
    ]
