import argparse
from pathlib import Path

from fastapi.testclient import TestClient

from epiq.cli import parser
from epiq.operations import operation_catalog
from epiq.web import create_app


def cli_commands() -> set[str]:
    root = parser()
    subparsers = next(
        action for action in root._actions if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices)


def test_every_cli_command_has_an_api_binding_or_explicit_exclusion() -> None:
    catalog = operation_catalog()
    assert {str(item["cli_command"]) for item in catalog} == cli_commands()
    assert len(catalog) == len(cli_commands())
    for item in catalog:
        assert item["api"] is not None or (item["equivalence"] == "excluded" and item["reason"])


def test_direct_operation_bindings_exist_in_fastapi(tmp_path: Path) -> None:
    app = create_app(tmp_path / "operations.sqlite", tmp_path / "missing")
    routes = {
        (method.upper(), route.path) for route in app.routes for method in (route.methods or set())
    }
    for item in operation_catalog():
        if item["api"] is None or item["equivalence"] == "family":
            continue
        assert (item["api"]["method"], item["api"]["path"]) in routes, item


def test_capabilities_expose_agent_safe_operations_and_epiql_check(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "capabilities.sqlite", tmp_path / "missing"))
    capabilities = client.get("/api/capabilities").json()
    by_command = {item["cli_command"]: item for item in capabilities["operation_catalog"]}
    assert by_command["entity"]["agent_available"] is True
    assert by_command["migrate"]["agent_available"] is False
    assert by_command["derive-distribution"]["api"] == {
        "method": "POST",
        "path": "/api/derive-distribution",
    }
    checked = client.post(
        "/api/epiql/check",
        json={"source": "question population : Int for Town { cardinality one }"},
    )
    assert checked.status_code == 200
    assert checked.json()["ok"] is True
