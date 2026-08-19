import json
from pathlib import Path

from epiq.html import snapshot_data, write_html
from epiq.store import Store


def _snapshot_from_html(document: str) -> dict:
    encoded = document.split("<script>const DATA=", 1)[1].split(";\nconst $=", 1)[0]
    return json.loads(encoded.replace("<\\/", "</"))


def test_html_snapshot_contains_the_whole_project_and_audit_state(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite"
    store = Store(database)
    store.initialize("Portable project")
    store.add_entity("Company", "Acorn </script><script>alert(1)</script>", {}, "test")
    store.add_entity("Founder", "Ada Founder", {}, "test")
    store.add_question("website", "Company", "URL", {"label": "Website"}, "test")
    _, evidence = store.add_evidence(
        "https://acorn.example/about",
        "Acorn about page",
        "2026-08-19",
        "The official website is https://acorn.example.",
        "test",
    )
    store.assert_claim(
        "Acorn </script><script>alert(1)</script>",
        "website",
        "https://acorn.example",
        "2026-08-19",
        [evidence],
        "test",
    )

    output = write_html(store, tmp_path / "snapshot.html")
    document = output.read_text(encoding="utf-8")
    snapshot = _snapshot_from_html(document)

    assert snapshot["snapshot_version"] == "1.0"
    assert snapshot["overview"]["project"]["name"] == "Portable project"
    assert {table["entity_kind"] for table in snapshot["tables"]} == {"Company", "Founder"}
    assert any(event["event_type"] == "claim.assert" for event in snapshot["events"])
    assert snapshot["integrity"]["ok"] is True
    assert document.count("</script>") == 1
    assert "fetch(" not in document
    assert "XMLHttpRequest" not in document
    assert 'method="post"' not in document.lower()
    assert 'id="wrap-toggle"' in document
    assert 'class="resize"' in document
    assert 'class="cell answered"' not in document
    assert "if(c.state==='Answered')return `<td class=\"inspect-cell\"" in document


def test_html_snapshot_can_be_scoped_or_export_an_empty_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "project.sqlite")
    store.initialize("Empty is inspectable")
    empty = snapshot_data(store)
    assert empty["tables"] == []
    assert write_html(store, tmp_path / "empty.html").exists()

    store.add_entity("Company", "Acorn", {}, "test")
    store.add_entity("Person", "Ada", {}, "test")
    scoped = snapshot_data(store, "Company")
    assert [table["entity_kind"] for table in scoped["tables"]] == ["Company"]
