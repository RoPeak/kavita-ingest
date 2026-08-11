from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationResult:
    valid: bool
    checks: tuple[str, ...]
    errors: tuple[str, ...] = ()

    def require_valid(self) -> None:
        if not self.valid:
            raise ValueError("verification failed: " + "; ".join(self.errors))
