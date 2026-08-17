"""Dependency-free XLSX export for Epiq projections."""

# ruff: noqa: E501 -- OOXML namespace and content-type literals are intentionally intact.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def _column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _text(value: Any) -> str:
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _cell(reference: str, value: Any, style: int = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{int(value)}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"{style_attr}><is><t xml:space="preserve">'
        f"{escape(_text(value))}</t></is></c>"
    )


def _worksheet(rows: list[list[Any]], widths: list[int]) -> str:
    body = []
    for row_number, row in enumerate(rows, 1):
        cells = "".join(
            _cell(f"{_column(column)}{row_number}", value, 1 if row_number == 1 else 0)
            for column, value in enumerate(row, 1)
        )
        body.append(f'<row r="{row_number}">{cells}</row>')
    last_column = _column(max((len(row) for row in rows), default=1))
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<cols>{columns}</cols><sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        f"</sheetView></sheetViews><sheetData>{''.join(body)}</sheetData>"
        f'<autoFilter ref="A1:{last_column}{max(len(rows), 1)}"/></worksheet>'
    )


def projection_rows(
    matrix: dict[str, Any], events: list[dict[str, Any]] | None = None
) -> dict[str, list[list[Any]]]:
    """Turn one projection and its audit history into Excel-friendly tables."""
    questions = matrix["questions"]
    data = [[matrix["entity_kind"], *[q["definition"].get("label", q["name"]) for q in questions]]]
    evidence = [
        [
            matrix["entity_kind"],
            "Question",
            "State",
            "Value",
            "Confidence",
            "Claim ID",
            "Evidence ID",
            "Source title",
            "Source URL",
            "Excerpt",
            "Provenance token",
            "Derivation",
            "Input claim IDs",
        ]
    ]
    unknowns = [[matrix["entity_kind"], "Question", "State", "Search query", "Notes"]]
    for entity in matrix["rows"]:
        values = []
        for question in questions:
            cell = entity["cells"][question["name"]]
            if cell["state"] == "Answered":
                value = cell.get("value", cell.get("values"))
            elif cell["state"] == "Contested":
                value = {"state": "Contested", "values": cell["values"]}
            else:
                value = f"[{cell['state']}]"
            values.append(value)
            for lineage in cell.get("lineage", []):
                evidence.append(
                    [
                        entity["name"],
                        question["name"],
                        cell["state"],
                        cell.get("value", cell.get("values")),
                        lineage.get("confidence"),
                        lineage.get("claim_id"),
                        lineage.get("evidence_id"),
                        lineage.get("source", {}).get("title"),
                        lineage.get("source", {}).get("url"),
                        lineage.get("excerpt"),
                        lineage.get("token"),
                        lineage.get("derivation", {}).get("operation"),
                        lineage.get("derivation", {}).get("input_claim_ids"),
                    ]
                )
            if cell["state"] != "Answered":
                research = cell.get("research", {})
                unknowns.append(
                    [
                        entity["name"],
                        question["name"],
                        cell["state"],
                        research.get("query"),
                        research.get("notes"),
                    ]
                )
        data.append([entity["name"], *values])
    schema = [["Field key", "Label", "Value type", "Cardinality", "Definition"]]
    for question in questions:
        schema.append(
            [
                question["name"],
                question["definition"].get("label", question["name"]),
                question["value_type"],
                question["definition"].get("cardinality", "one"),
                question["definition"],
            ]
        )
    event_log = [["Sequence", "Event ID", "Recorded at", "Actor", "Event type", "Payload"]]
    for event in events or []:
        event_log.append(
            [
                event.get("seq"),
                event.get("event_id"),
                event.get("recorded_at"),
                event.get("actor"),
                event.get("event_type"),
                event.get("payload"),
            ]
        )
    return {
        "Table": data,
        "Evidence": evidence,
        "Research Gaps": unknowns,
        "Field Schema": schema,
        "Event Log": event_log,
    }


def write_xlsx(
    matrix: dict[str, Any], output: str | Path, events: list[dict[str, Any]] | None = None
) -> Path:
    """Write a multi-sheet audit workbook using only the standard library."""
    tables = projection_rows(matrix, events)
    sheets = list(tables)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content_types = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheets, 1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    relationships += (
        f'<Relationship Id="rId{len(sheets) + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{content_types}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{workbook_sheets}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relationships}</Relationships>",
        )
        archive.writestr(
            "xl/styles.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font/><font><b/><color rgb="FFFFFFFF"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF084A3C"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf/><xf fontId="1" fillId="1" applyFont="1" applyFill="1"/></cellXfs>'
            "</styleSheet>",
        )
        widths = {
            "Table": [22] + [24] * (len(tables["Table"][0]) - 1),
            "Evidence": [22, 24, 14, 28, 12, 22, 22, 32, 48, 80, 28, 22, 60],
            "Research Gaps": [22, 24, 14, 48, 80],
            "Field Schema": [24, 32, 22, 16, 80],
            "Event Log": [12, 24, 24, 24, 28, 100],
        }
        for index, name in enumerate(sheets, 1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml", _worksheet(tables[name], widths[name])
            )
    return output_path.resolve()
