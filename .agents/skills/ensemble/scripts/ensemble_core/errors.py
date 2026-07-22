from __future__ import annotations


class EnsembleError(Exception):
    code = "ENSEMBLE_ERROR"

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"error": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


class InputError(EnsembleError):
    code = "INPUT_ERROR"


class SchemaError(EnsembleError):
    code = "SCHEMA_ERROR"


class SemanticValidationError(EnsembleError):
    code = "SEMANTIC_VALIDATION_ERROR"


class InfraError(EnsembleError):
    code = "INFRA_ERROR"


class StateError(EnsembleError):
    code = "STATE_ERROR"


class SecurityError(EnsembleError):
    code = "SECURITY_ERROR"
