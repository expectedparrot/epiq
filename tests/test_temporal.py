from pathlib import Path

import pytest

from epiq.errors import EpiqError
from epiq.store import Store
from epiq.temporal import instant


@pytest.fixture
def project(tmp_path: Path):
    store = Store(tmp_path / "time.sqlite")
    store.initialize("Time")
    store.add_entity("X", "A", {}, "setup")
    store.add_question("q", "X", "String", {}, "setup")
    evidence = store.add_evidence("https://example.test", "Test", "2026-09-09", "v", "setup")[1]
    return store, evidence


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    ("start", "cutoff", "expected"),
    [
        ("2018-01-13T08:07:00-10:00", "2018-01-13T18:06:00Z", "Unasked"),
        ("2018-01-13T20:07:00+02:00", "2018-01-13T18:07:00Z", "Answered"),
        ("2018-01-13T18:07:00.1Z", "2018-01-13T18:07:00Z", "Unasked"),
        ("2018-01-13T18:07:00.1Z", "2018-01-13T18:07:00.100000Z", "Answered"),
        ("2018-01-13T00:00:00Z", "2018-01-13", "Answered"),
        ("2018-01-13T00:00:00.000001Z", "2018-01-13", "Unasked"),
    ],
)
def test_temporal_comparisons(project, monkeypatch, legacy, start, cutoff, expected):
    store, evidence = project
    if legacy:
        # Simulate the old writer, which preserved timestamps verbatim.
        monkeypatch.setattr("epiq.store.normalize", lambda value, **kwargs: value)
    claim = store.assert_claim("A", "q", "v", start, evidence, "test")
    monkeypatch.undo()
    history = store.history()
    cell = store.matrix("X", valid_at=cutoff)["rows"][0]["cells"]["q"]
    assert cell["state"] == expected
    assert store.history() == history  # Compatibility reads never rewrite ledger events.
    assert store.assert_claim("A", "q", "v", instant(start), evidence, "test") == claim


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-date",
        "2026-02-30",
        "2026-09-09T12:00:00",
        "2026-09-09T12:00:00.1234567Z",
        "9999-12-31T23:59:59-02:00",
    ],
)
def test_invalid_timestamps_are_domain_errors_and_writes_are_atomic(project, bad):
    store, evidence = project
    before = store.history()
    for field in ("valid_from", "recorded_at"):
        kwargs = {"valid_from": "2026-09-09", field: bad}
        with pytest.raises(EpiqError) as error:
            store.assert_claim("A", "q", "v", evidence_id=evidence, actor="test", **kwargs)
        assert error.value.code == "invalid_timestamp"
    for field in ("valid_at", "known_at"):
        with pytest.raises(EpiqError) as error:
            store.matrix("X", **{field: bad})
        assert error.value.code == "invalid_timestamp"
    assert store.history() == before


def test_validity_end_compares_instants_and_excludes_equality(project):
    store, evidence = project
    claim = store.assert_claim("A", "q", "v", "2018-01-13T20:07:00+02:00", evidence, "test")
    with pytest.raises(EpiqError, match="after valid_from"):
        store.end_claim_validity(claim, "2018-01-13T08:07:00-10:00", "same instant", "test")
    store.end_claim_validity(claim, "2018-01-13T08:08:00-10:00", "ended", "test")
    assert (
        store.matrix("X", valid_at="2018-01-13T18:07:59.999999Z")["rows"][0]["cells"]["q"]["state"]
        == "Answered"
    )
    assert (
        store.matrix("X", valid_at="2018-01-13T18:08:00Z")["rows"][0]["cells"]["q"]["state"]
        == "Unasked"
    )


def test_historical_links_and_retraction_with_legacy_event_times(project, monkeypatch):
    store, evidence = project
    monkeypatch.setattr("epiq.store.normalize", lambda value, **kwargs: value)
    monkeypatch.setattr(store, "_timestamp", lambda: "2099-01-01T02:00:00+02:00")
    claim = store.assert_claim("A", "q", "v", "2018-01-01", evidence, "test")
    monkeypatch.setattr(store, "_timestamp", lambda: "2099-01-01T00:00:00.1Z")
    extra = store.add_evidence(
        "https://example.test/extra", "Extra", "2026-09-09", "v too", "test"
    )[1]
    store.assert_claim("A", "q", "v", "2018-01-01", [evidence, extra], "test")
    monkeypatch.setattr(store, "_timestamp", lambda: "2099-01-01T00:00:00.2Z")
    store.close_claim(claim, "retracted", "incorrect", "test")
    monkeypatch.undo()

    def cell(cutoff):
        return store.matrix("X", known_at=cutoff)["rows"][0]["cells"]["q"]

    assert len(cell("2099-01-01T00:00:00Z")["lineage"]) == 1
    assert len(cell("2099-01-01T00:00:00.100000Z")["lineage"]) == 2
    assert cell("2099-01-01T00:00:00.200000Z")["state"] == "Unasked"


def test_doctor_reports_legacy_invalid_time_without_rewriting(project):
    store, evidence = project
    claim = store.assert_claim("A", "q", "v", "2018-01-01", evidence, "test")
    with store.transaction() as connection:
        connection.execute("UPDATE claims SET valid_from=? WHERE claim_id=?", ("bad-date", claim))
    before = store.history()
    report = store.doctor()
    assert not report["ok"]
    assert report["findings"][0]["code"] == "invalid_timestamp"
    with pytest.raises(EpiqError) as error:
        store.matrix("X")
    assert error.value.code == "invalid_timestamp"
    assert store.history() == before
