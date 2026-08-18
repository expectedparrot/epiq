"""Background research runner adapters."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

Progress = Callable[[str], None]


class ResearchRunner(Protocol):
    """A replaceable agent capable of researching one question for several entities."""

    def __call__(
        self,
        entity_kind: str,
        question: dict[str, Any],
        entities: list[dict[str, Any]],
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]: ...


class EntitySuggestionRunner(Protocol):
    """A replaceable agent that proposes new members of an entity set."""

    def __call__(
        self,
        entity_kind: str,
        existing_entities: list[dict[str, Any]],
        count: int,
        instructions: str,
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]: ...


class FieldSuggestionRunner(Protocol):
    """A replaceable agent that proposes complementary research fields."""

    def __call__(
        self,
        entity_kind: str,
        existing_questions: list[dict[str, Any]],
        sample_entities: list[dict[str, Any]],
        count: int,
        instructions: str,
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]: ...


class WorkspaceAgentRunner(Protocol):
    """Plan bounded schema, entity, and research actions for a workspace goal."""

    def __call__(
        self,
        goal: str,
        context: dict[str, Any],
        progress: Progress | None = None,
    ) -> dict[str, Any]: ...


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "entity_id",
                    "status",
                    "value_json",
                    "source_type",
                    "source_url",
                    "source_published_at",
                    "observed_as_of",
                    "source_title",
                    "excerpt",
                    "confidence",
                    "notes",
                ],
                "properties": {
                    "entity_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["answered", "not_found"]},
                    "value_json": {"type": "string"},
                    "source_type": {
                        "type": "string",
                        "enum": ["web", "model", "report", "interview", "other"],
                    },
                    "source_url": {"type": ["string", "null"]},
                    "source_published_at": {"type": ["string", "null"]},
                    "observed_as_of": {"type": ["string", "null"]},
                    "source_title": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "notes": {"type": "string"},
                },
            },
        }
    },
}

SUGGESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "rationale", "source_title", "source_url"],
                "properties": {
                    "name": {"type": "string"},
                    "rationale": {"type": "string"},
                    "source_title": {"type": "string"},
                    "source_url": {"type": "string"},
                },
            },
        }
    },
}

FIELD_SUGGESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggestions"],
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "name",
                    "label",
                    "value_type",
                    "rationale",
                    "research_guidance",
                ],
                "properties": {
                    "name": {"type": "string"},
                    "label": {"type": "string"},
                    "value_type": {"type": "string"},
                    "rationale": {"type": "string"},
                    "research_guidance": {"type": "string"},
                },
            },
        }
    },
}

WORKSPACE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "entity_kinds", "entities", "questions", "research"],
    "properties": {
        "summary": {"type": "string"},
        "entity_kinds": {"type": "array", "items": {"type": "string"}},
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name"],
                "properties": {
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "kind",
                    "name",
                    "value_type",
                    "label",
                    "cardinality",
                    "volatility",
                    "freshness_days",
                    "research_guidance",
                ],
                "properties": {
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "value_type": {"type": "string"},
                    "label": {"type": "string"},
                    "cardinality": {"type": "string", "enum": ["one", "many"]},
                    "volatility": {
                        "type": "string",
                        "enum": ["stable", "slow", "dynamic"],
                    },
                    "freshness_days": {"type": ["integer", "null"]},
                    "research_guidance": {"type": "string"},
                },
            },
        },
        "research": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "question", "entity_names", "instructions"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "Exact existing or proposed entity-kind identifier.",
                    },
                    "question": {
                        "type": "string",
                        "description": "Exact existing or proposed question name; never prose.",
                    },
                    "entity_names": {"type": "array", "items": {"type": "string"}},
                    "instructions": {
                        "type": "string",
                        "description": "Natural-language source and interpretation instructions.",
                    },
                },
            },
        },
    },
}


class CodexResearchRunner:
    """Run an ephemeral, read-only Codex session and require structured findings."""

    def __init__(self, working_directory: str | Path | None = None) -> None:
        self.working_directory = Path(working_directory or Path.cwd())

    def __call__(
        self,
        entity_kind: str,
        question: dict[str, Any],
        entities: list[dict[str, Any]],
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]:
        if progress:
            progress("Starting a read-only Codex research session")
        prompt = f"""Research a typed database column for Epiq.

Row type: {entity_kind}
Question: {json.dumps(question, sort_keys=True)}
Rows: {json.dumps(entities, sort_keys=True)}

Research each row independently using public sources. Return exactly one result per entity_id.
Use question.research_context to disambiguate every row. Project name, peer rows, aliases, and row
attributes are identity constraints, not incidental hints. Before accepting a source, verify that
it describes the intended entity in that project and table. A matching bare name is insufficient
when places, people, organizations, or works can share names. If identity cannot be verified,
return not_found and explain the ambiguity.
The value_json field must contain a JSON-encoded value conforming to value_type (for example,
Bool true is the string "true", and a String is the string "\"Boston\""). For answered
results, include a direct source URL, descriptive title, and a short supporting excerpt. Do not
infer a positive or negative answer from absence. If sufficient evidence cannot be found, use
status not_found, value_json "null", and explain the searches in notes. Never modify local files.
"""
        with tempfile.TemporaryDirectory(prefix="epiq-research-") as directory:
            temp = Path(directory)
            schema_path = temp / "schema.json"
            output_path = temp / "result.json"
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA))
            process = subprocess.run(
                [
                    "codex",
                    "exec",
                    "--ephemeral",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    "--cd",
                    str(self.working_directory),
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            if process.returncode != 0:
                detail = process.stderr.strip() or process.stdout.strip() or "Codex failed"
                raise RuntimeError(detail[-2000:])
            result = json.loads(output_path.read_text())
        if progress:
            progress("Codex returned structured findings")
        return _parse_values(result["results"])


class OpenAIResearchRunner:
    """Research through the OpenAI Responses API with hosted web search."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str = "https://api.openai.com/v1/responses",
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("EPIQ_RESEARCH_MODEL", "gpt-5.6-terra")
        self.endpoint = endpoint

    def __call__(
        self,
        entity_kind: str,
        question: dict[str, Any],
        entities: list[dict[str, Any]],
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        prompt = f"""Research this Epiq database column using public web sources.

Row type: {entity_kind}
Question: {json.dumps(question, sort_keys=True)}
Rows: {json.dumps(entities, sort_keys=True)}

Task mode and user instructions are included in the question object. Treat explicit user source
instructions as binding: if the user names a site or database, search that source specifically. If
it cannot be accessed, return not_found and explain that limitation instead of silently
substituting an unrelated aggregator. In add_evidence mode, inspect a new independent source and
return the value that source actually reports. It may support the existing_value or conflict with
it; never suppress, coerce, or discard a conflicting observation.
Use question.research_context to disambiguate every row. Project name, peer rows, aliases, and row
attributes are identity constraints, not incidental hints. Before accepting a source, verify that
it describes the intended entity in that project and table. A matching bare name is insufficient
when places, people, organizations, or works can share names. If identity cannot be verified,
return not_found and explain the ambiguity.
Each row supplies existing_evidence with every URL, title, and excerpt already attached. Do not
return any existing URL, the same page under another URL variant, or merely another excerpt from an
existing source. If no independent source can be found, return not_found rather than duplicating it.
Treat definition.research_guidance as a binding interpretation rule, including distinctions the
human has identified after reviewing earlier mistakes.
Return exactly one result for every supplied entity_id. The value_json field must contain the
conforming value encoded as JSON text. For status not_found, value_json must be the literal JSON
text "null"; never return an empty string. For any question with cardinality many, return a JSON
array
whose elements individually conform to value_type. For example, a many-valued String may return
["SaaS","Healthcare"], and a many-valued Enum[a,b,c] may return ["a","c"]. Epiq stores these as
separate scalar claims in one cell. For a Ref[Type] question with cardinality many, return every
directly supported related entity either as a name or as an object with a required "name" and
additional keys matching fields on the referenced table. For example,
[{{"name":"Ada Lovelace","birth_year":1815}}] can propose an Author row and populate its typed
birth_year field after approval. Every additional value must conform to that referenced field's
type; do not invent keys or enum choices. Epiq will resolve existing entities and stage missing
ones for human approval. For answered results, cite a
direct source URL, title, and a
short excerpt that directly supports the value. Do not treat absence of evidence as a negative
answer. Provide the source publication date and the date the claim was observed to hold when they
can be established; do not substitute today's date for an unknown historical date. Use not_found
with detailed search notes when evidence is insufficient.
In retry_not_found mode, address the human correction in the instructions. Absence from a source
may support a negative value only when the source is authoritative and demonstrably exhaustive for
the relevant population and time; cite that source and explain the closed-world inference.
"""
        payload = {
            "model": self.model,
            "input": prompt,
            "tools": [{"type": "web_search"}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "epiq_research_results",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                }
            },
            "stream": True,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                result = None
                if progress:
                    progress(f"Connected to OpenAI · {self.model}")
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    event_type = str(event.get("type", ""))
                    if "web_search_call" in event_type and event_type.endswith("in_progress"):
                        if progress:
                            progress("Searching the web")
                    elif "web_search_call" in event_type and event_type.endswith("completed"):
                        if progress:
                            progress("Web search completed; reviewing sources")
                    elif event_type == "response.completed":
                        result = event["response"]
                        if progress:
                            progress("Validating structured findings")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"OpenAI API returned {error.code}: {detail[-2000:]}") from error
        if result is None:
            raise RuntimeError("OpenAI stream ended without a completed response")
        text = next(
            content["text"]
            for output in result.get("output", [])
            if output.get("type") == "message"
            for content in output.get("content", [])
            if content.get("type") == "output_text"
        )
        if not text.strip():
            raise RuntimeError("OpenAI completed without structured research output")
        try:
            response_payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "OpenAI returned invalid structured research JSON: "
                f"{error.msg} at line {error.lineno} column {error.colno}"
            ) from error
        return _parse_values(response_payload["results"])


class OpenAIWorkspaceAgentRunner(OpenAIResearchRunner):
    """Turn a natural-language workspace goal into validated Epiq primitives."""

    def __call__(
        self,
        goal: str,
        context: dict[str, Any],
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        prompt = f"""Design the next concrete actions for an Epiq evidence-backed workspace.

User direction: {goal}
Current workspace: {json.dumps(context, sort_keys=True)}

Return a fast, schema-first starter plan. Do not perform web research during this planning step.
Epiq models durable things as entity
rows, typed questions as columns, and one-to-many facts (funding rounds, press articles, writings,
observations) as related entity tables rather than JSON arrays. Prefer one primary table plus only
the related tables genuinely needed by the user's goal. Use Ref[Kind] questions for relationships.

You may propose only additive operations: entity kinds, entities, questions, and research requests.
Do not rename, delete, retire, supersede, or invent claims. Reuse existing schema and rows. For an
empty workspace, propose at most 3 tables, 5 recognizable seed rows, 8 initial fields, and 24
initial research cells. Seed rows are a bounded starting set, never a claim of exhaustive coverage.
Broader entity discovery happens incrementally after approval. Research only the cells needed to
make the starter workspace useful. Question names must be snake_case. Use Epiq
value types such as String, Bool, Int,
Float, Year, Date, URL, Probability, Enum[a,b], Quantity[USD], and Ref[Kind]. Put a concise label,
cardinality, volatility, freshness_days when appropriate, and precise research_guidance in each
definition. Research requests must refer to a proposed or existing question and row. For a
relationship field, research can return provisional related rows for human review.

For every research request, kind MUST be the exact entity-kind identifier from the workspace and
question MUST be one exact snake_case field name on that table. Put search prose only in
instructions. To verify several fields, emit one research request per field; never invent task
categories such as "primary_source_verification" as a kind or put a search query in question.

The summary should tell the user what you chose to create and why. Do not imply that requested
research is already complete.
"""
        payload = {
            "model": self.model,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "epiq_workspace_plan",
                    "strict": True,
                    "schema": WORKSPACE_PLAN_SCHEMA,
                }
            },
            "stream": True,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                result = None
                if progress:
                    progress(f"Connected to OpenAI · {self.model}")
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    event_type = str(event.get("type", ""))
                    if "web_search_call" in event_type and event_type.endswith("in_progress"):
                        if progress:
                            progress("Finding representative entities and sources")
                    elif "web_search_call" in event_type and event_type.endswith("completed"):
                        if progress:
                            progress("Web search completed; designing the workspace")
                    elif event_type == "response.completed":
                        result = event["response"]
                        if progress:
                            progress("Validating the proposed workspace plan")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"OpenAI API returned {error.code}: {detail[-2000:]}") from error
        if result is None:
            raise RuntimeError("OpenAI stream ended without a completed response")
        text = next(
            content["text"]
            for output in result.get("output", [])
            if output.get("type") == "message"
            for content in output.get("content", [])
            if content.get("type") == "output_text"
        )
        return json.loads(text)


class OpenAIEntitySuggestionRunner(OpenAIResearchRunner):
    """Discover candidate rows through hosted web search without saving them."""

    def __call__(
        self,
        entity_kind: str,
        existing_entities: list[dict[str, Any]],
        count: int,
        instructions: str,
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        existing_names = [str(item["name"]) for item in existing_entities]
        prompt = f"""Suggest additional entities for an Epiq research table.

Entity type: {entity_kind}
Existing rows (do not repeat these): {json.dumps(existing_names)}
Number requested: {count}
Selection instructions: {instructions or "Find representative, well-supported examples."}

Return plausible members of the requested entity type, not categories or placeholder names. Give
one concise reason each belongs in this table and one public source that establishes its identity
or relevance. Suggestions are provisional and will be reviewed by a human before being added.
"""
        payload = {
            "model": self.model,
            "input": prompt,
            "tools": [{"type": "web_search"}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "epiq_entity_suggestions",
                    "strict": True,
                    "schema": SUGGESTION_SCHEMA,
                }
            },
            "stream": True,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                result = None
                if progress:
                    progress(f"Connected to OpenAI · {self.model}")
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    event_type = str(event.get("type", ""))
                    if "web_search_call" in event_type and event_type.endswith("in_progress"):
                        if progress:
                            progress("Searching for candidates")
                    elif "web_search_call" in event_type and event_type.endswith("completed"):
                        if progress:
                            progress("Reviewing candidate sources")
                    elif event_type == "response.completed":
                        result = event["response"]
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"OpenAI API returned {error.code}: {detail[-2000:]}") from error
        if result is None:
            raise RuntimeError("OpenAI stream ended without a completed response")
        text = next(
            content["text"]
            for output in result.get("output", [])
            if output.get("type") == "message"
            for content in output.get("content", [])
            if content.get("type") == "output_text"
        )
        return list(json.loads(text)["suggestions"])


class OpenAIFieldSuggestionRunner(OpenAIResearchRunner):
    """Propose useful new columns from the table's existing schema and rows."""

    def __call__(
        self,
        entity_kind: str,
        existing_questions: list[dict[str, Any]],
        sample_entities: list[dict[str, Any]],
        count: int,
        instructions: str,
        progress: Progress | None = None,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        prompt = f"""Suggest additional fields for an Epiq research table.

Row type: {entity_kind}
Existing fields (do not repeat or paraphrase these): {json.dumps(existing_questions)}
Example rows, for context only: {json.dumps(sample_entities)}
Number requested: {count}
Selection instructions: {
            instructions or "Propose complementary, decision-useful research questions."
        }

Each suggestion must be a field that can be answered independently for every row. Use a unique
snake_case name, a concise human-readable label, and one valid Epiq value_type: String, URL, Int,
Float, Probability, Bool, Json, Enum[a,b,c], Distribution[Float], or
Distribution[Enum[a,b,c]]. Prefer
precise typed fields over broad biography-style prompts. In research_guidance, define ambiguous
terms and state what evidence would count so a later research agent applies the field consistently.
Suggestions are provisional and will be reviewed by a human before becoming columns. No web search
is needed: reason from the supplied schema and table context.
"""
        payload = {
            "model": self.model,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "epiq_field_suggestions",
                    "strict": True,
                    "schema": FIELD_SUGGESTION_SCHEMA,
                }
            },
            "stream": True,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                result = None
                if progress:
                    progress(f"Connected to OpenAI · {self.model}")
                for raw_line in response:
                    line = raw_line.decode().strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    if event.get("type") == "response.completed":
                        result = event["response"]
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"OpenAI API returned {error.code}: {detail[-2000:]}") from error
        if result is None:
            raise RuntimeError("OpenAI stream ended without a completed response")
        text = next(
            content["text"]
            for output in result.get("output", [])
            if output.get("type") == "message"
            for content in output.get("content", [])
            if content.get("type") == "output_text"
        )
        return list(json.loads(text)["suggestions"])


def _parse_values(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the strict wire representation into Epiq's typed Python values."""
    normalized = []
    for result in results:
        item = dict(result)
        entity_id = str(item.get("entity_id") or "unknown entity")
        status = str(item.get("status") or "unknown")
        raw_value = item.pop("value_json", None)
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            if status == "not_found":
                item["value"] = None
                normalized.append(item)
                continue
            raise RuntimeError(
                f"Research result for {entity_id} has status {status} but an empty value_json"
            )
        if not isinstance(raw_value, str):
            raise RuntimeError(
                f"Research result for {entity_id} has a non-string value_json"
            )
        try:
            item["value"] = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Research result for {entity_id} has invalid value_json: "
                f"{error.msg} at line {error.lineno} column {error.colno}"
            ) from error
        if status == "not_found" and item["value"] is not None:
            raise RuntimeError(
                f"Research result for {entity_id} has status not_found but a non-null value_json"
            )
        normalized.append(item)
    return normalized
