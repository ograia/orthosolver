from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class FatalError:
    error_class: str
    message: str
    diagnostics: list[str] = field(default_factory=list)
    error_scope: str = "input"
    status: Literal["fatal"] = "fatal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error_class": self.error_class,
            "error_scope": self.error_scope,
            "message": self.message,
            "diagnostics": list(self.diagnostics),
        }


class NormalizationError(Exception):
    def __init__(self, error: FatalError):
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class OkResult(Generic[T]):
    data: T
    status: Literal["ok"] = "ok"


@dataclass(frozen=True)
class FatalResult:
    error: FatalError
    status: Literal["fatal"] = "fatal"

    def to_dict(self) -> dict[str, Any]:
        return self.error.to_dict()


NormalizationResult = OkResult[T] | FatalResult
