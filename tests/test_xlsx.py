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

    tables = projection_rows(matrix)
    assert tables["Data"][1] == ["Example", 42, "[NotFound]"]
    assert tables["Evidence"][1][8] == "https://example.test"
    assert tables["Unknowns"][1][-1] == "No sufficient result."

    output = write_xlsx(matrix, tmp_path / "report.xlsx")
    with ZipFile(output) as archive:
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))
