"""Domain errors exposed by Epiq."""


class EpiqError(Exception):
    """A machine-readable domain error."""

    def __init__(self, code: str, message: str, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion

    def as_dict(self) -> dict[str, str]:
        """Return the public JSON error representation."""
        result = {"code": self.code, "message": self.message}
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result
