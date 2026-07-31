from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


COUNCIL_STAGES = frozenset({"proposal", "jury", "synthesis"})


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    PROVIDER_SERVER = "provider_server"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    CONTENT_FILTER = "content_filter"
    BUDGET = "budget"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> Usage:
        value = value or {}
        return cls(
            input_tokens=value.get("input_tokens"),
            output_tokens=value.get("output_tokens"),
            total_tokens=value.get("total_tokens"),
            cached_input_tokens=value.get("cached_input_tokens"),
            reasoning_tokens=value.get("reasoning_tokens"),
        )


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    lineage: str
    secret_name: str
    endpoint: str
    max_output_tokens: int = 1800
    timeout_seconds: float = 90.0
    max_attempts: int = 3
    enabled: bool = True
    stage_max_output_tokens: dict[str, int] = field(default_factory=dict)
    stage_timeout_seconds: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def output_tokens_for(self, stage: str) -> int:
        return int(
            self.stage_max_output_tokens.get(stage, self.max_output_tokens)
        )

    def timeout_for(self, stage: str) -> float:
        return float(
            self.stage_timeout_seconds.get(stage, self.timeout_seconds)
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProviderConfig:
        return cls(
            name=str(value["name"]),
            model=str(value["model"]),
            lineage=str(value["lineage"]),
            secret_name=str(value["secret_name"]),
            endpoint=str(value["endpoint"]),
            max_output_tokens=int(value.get("max_output_tokens", 1800)),
            timeout_seconds=float(value.get("timeout_seconds", 90.0)),
            max_attempts=int(value.get("max_attempts", 3)),
            enabled=bool(value.get("enabled", True)),
            stage_max_output_tokens={
                str(stage): int(limit)
                for stage, limit in dict(
                    value.get("stage_max_output_tokens") or {}
                ).items()
            },
            stage_timeout_seconds={
                str(stage): float(limit)
                for stage, limit in dict(
                    value.get("stage_timeout_seconds") or {}
                ).items()
            },
            extra=dict(value.get("extra") or {}),
        )


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    resolved_model: str
    request_id: str | None
    usage: Usage
    latency_ms: int
    attempts: int
    finish_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["usage"] = self.usage.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ProviderResponse:
        return cls(
            content=str(value["content"]),
            resolved_model=str(value["resolved_model"]),
            request_id=value.get("request_id"),
            usage=Usage.from_dict(value.get("usage")),
            latency_ms=int(value.get("latency_ms", 0)),
            attempts=int(value.get("attempts", 1)),
            finish_reason=value.get("finish_reason"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class RunPolicy:
    proposal_quorum: int = 3
    jury_quorum: int = 3
    min_lineages: int = 3
    max_calls: int = 16
    deadline_seconds: float = 480.0
    allow_partial: bool = True
    max_question_chars: int = 30_000
    max_stage_prompt_chars: int = 60_000
    truncation_retries: int = 1
    max_recovery_output_tokens: int = 8_192

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> RunPolicy:
        value = value or {}
        return cls(
            proposal_quorum=int(value.get("proposal_quorum", 3)),
            jury_quorum=int(value.get("jury_quorum", 3)),
            min_lineages=int(value.get("min_lineages", 3)),
            max_calls=int(value.get("max_calls", 16)),
            deadline_seconds=float(value.get("deadline_seconds", 480.0)),
            allow_partial=bool(value.get("allow_partial", True)),
            max_question_chars=int(value.get("max_question_chars", 30_000)),
            max_stage_prompt_chars=int(
                value.get("max_stage_prompt_chars", 60_000)
            ),
            truncation_retries=int(value.get("truncation_retries", 1)),
            max_recovery_output_tokens=int(
                value.get("max_recovery_output_tokens", 8_192)
            ),
        )


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
        retryable: bool = False,
        status_code: int | None = None,
        request_id: str | None = None,
        attempts: int = 1,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.request_id = request_id
        self.attempts = attempts
        self.ambiguous = ambiguous

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "category": self.category.value,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "attempts": self.attempts,
            "ambiguous": self.ambiguous,
        }
