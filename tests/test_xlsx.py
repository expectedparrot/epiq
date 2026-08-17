from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from epiq.store import Store
from epiq.xlsx import projection_rows, write_xlsx


def test_xlsx_preserves_values_evidence_and_unknowns(tmp_path: Path) -> None:
    store = Store(tmp_path / "research.sqlite")
    store.initialize("Research")
    town = store.add_entity("Town", "Example", {}, "test")
    store.add_question("population", "Town", "Int", {"label": "Population"}, "test")
    store.add_question("price", "Town", "Int", {"label": "Price"}, "test")
    _, evidence = store.add_evidence(
        "https://example.test", "Official data", "2026-01-01", "Population is 42.", "test"
    )
    store.assert_claim(town, "population", 42, "2025-01-01", evidence, "test")
    store.record_not_found(town, "price", "Example price", "No sufficient result.", "test")
    matrix = store.matrix("Town")

    tables = projection_rows(matrix, store.history())
    assert tables["Table"][1] == ["Example", 42, "[NotFound]"]
    assert tables["Evidence"][1][8] == "https://example.test/"
    assert tables["Research Gaps"][1][-1] == "No sufficient result."
    assert tables["Field Schema"][1][:3] == ["population", "Population", "Int"]
    assert any(row[4] == "claim.assert" for row in tables["Event Log"][1:])

    output = write_xlsx(matrix, tmp_path / "report.xlsx", store.history())
    with ZipFile(output) as archive:
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
        workbook = archive.read("xl/workbook.xml").decode()
        for sheet in ["Table", "Evidence", "Research Gaps", "Field Schema", "Event Log"]:
            assert f'name="{sheet}"' in workbook
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))
