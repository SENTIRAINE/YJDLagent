from __future__ import annotations


class SensitiveHeaders(dict[str, str]):
    """Mapping whose diagnostic representation never exposes credentials."""

    def __repr__(self) -> str:
        safe = {
            key: "Bearer [REDACTED]"
            if key.lower() == "authorization"
            else value
            for key, value in self.items()
        }
        return repr(safe)

    __str__ = __repr__
