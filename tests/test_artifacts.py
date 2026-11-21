from pathlib import Path

import pytest

from epiq.artifacts import ArtifactStore
from epiq.errors import EpiqError


def test_artifacts_are_content_addressed_idempotent_and_verified(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    first = store.retain(b"report", retention="git")
    second = store.retain(b"report", retention="git-lfs")

    assert first.digest == second.digest
    assert first.path == second.path
    assert store.read(first.digest) == b"report"
    assert store.verify_all()[0].digest == first.digest

    assert first.path is not None
    (tmp_path / first.path).write_bytes(b"tampered")
    with pytest.raises(EpiqError) as error:
        store.verify(first.digest)
    assert error.value.code == "artifact_digest_mismatch"


def test_external_artifact_records_hash_without_retaining_bytes(tmp_path: Path) -> None:
    reference = ArtifactStore(tmp_path).retain(b"external", retention="external")
    assert reference.path is None
    assert not (tmp_path / "artifacts").exists()
