"""Content-addressed retained artifacts for Git-native Epiq projects."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import EpiqError

_DIGEST = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_STORED_MODES = {"git", "git-lfs"}
_MODES = _STORED_MODES | {"external"}


@dataclass(frozen=True)
class ArtifactReference:
    digest: str
    retention: str
    size: int
    path: str | None

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "digest": self.digest,
            "retention": self.retention,
            "size": self.size,
            "path": self.path,
        }


class ArtifactStore:
    """Atomically retain and verify SHA-256 addressed bytes."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.root = self.project_root / "artifacts" / "sha256"

    def retain(self, content: bytes, *, retention: str = "git") -> ArtifactReference:
        if retention not in _MODES:
            raise EpiqError("invalid_artifact_retention", f"Unknown retention mode: {retention}")
        hexadecimal = hashlib.sha256(content).hexdigest()
        digest = f"sha256:{hexadecimal}"
        if retention == "external":
            return ArtifactReference(digest, retention, len(content), None)
        destination = self._path(hexadecimal)
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != content:
                raise EpiqError("artifact_digest_collision", f"Invalid artifact at {destination}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{hexadecimal}.", dir=destination.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return ArtifactReference(
            digest,
            retention,
            len(content),
            str(destination.relative_to(self.project_root)),
        )

    def verify(self, digest: str) -> ArtifactReference:
        hexadecimal = self._hex(digest)
        path = self._path(hexadecimal)
        if not path.is_file() or path.is_symlink():
            raise EpiqError("artifact_missing", f"Artifact is not retained: sha256:{hexadecimal}")
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != hexadecimal:
            raise EpiqError(
                "artifact_digest_mismatch",
                f"Artifact sha256:{hexadecimal} contains bytes hashing to sha256:{actual}",
            )
        return ArtifactReference(
            f"sha256:{hexadecimal}",
            "git",
            len(content),
            str(path.relative_to(self.project_root)),
        )

    def verify_all(self) -> list[ArtifactReference]:
        if not self.root.exists():
            return []
        references = []
        for path in sorted(self.root.glob("*/*")):
            if path.is_file() or path.is_symlink():
                references.append(self.verify(path.name))
        return references

    def read(self, digest: str) -> bytes:
        reference = self.verify(digest)
        assert reference.path is not None
        return (self.project_root / reference.path).read_bytes()

    def _path(self, hexadecimal: str) -> Path:
        return self.root / hexadecimal[:2] / hexadecimal

    @staticmethod
    def _hex(digest: str) -> str:
        match = _DIGEST.fullmatch(digest)
        if not match:
            raise EpiqError("invalid_artifact_digest", f"Invalid SHA-256 digest: {digest}")
        return match.group(1)
