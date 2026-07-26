from __future__ import annotations

import hashlib
import json
import re

from model_council.models import ProviderConfig, ProviderResponse, Usage

from .base import Provider


_LABELS_RE = re.compile(
    r"Allowed candidate labels:\s*(\[.*?\])\s*"
    r"BEGIN_UNTRUSTED_EVALUATION_JSON",
    re.IGNORECASE | re.DOTALL,
)


class MockProvider(Provider):
    """Deterministic offline provider used by smoke tests and demos."""

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str = "mock-only-sentinel",
    ) -> None:
        super().__init__(config, api_key)
        self._seed = int(config.extra.get("seed", 0))

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
    ) -> ProviderResponse:
        digest = hashlib.sha256(
            (
                f"{self._seed}\0{self.config.name}\0{stage}\0"
                f"{system_prompt}\0{user_prompt}"
            ).encode("utf-8")
        ).hexdigest()
        if stage == "jury":
            content = self._jury_content(user_prompt)
        elif stage == "synthesis":
            content = (
                "## Outcome\n"
                "The deterministic mock council completed its synthesis.\n\n"
                "## Consensus\n"
                "The preserved aggregate controls the result.\n\n"
                "## Dissent\n"
                "See the recorded mock jury rationales.\n\n"
                "## Verification needed\n"
                "Replace mock providers with configured external providers."
            )
        else:
            content = (
                "## Outcome\n"
                f"Deterministic mock proposal {digest[:12]}.\n\n"
                "## Evidence and reasoning\n"
                "This response verifies the offline council execution path.\n\n"
                "## Uncertainty\n"
                "It contains no external factual verification.\n\n"
                "## Verification needed\n"
                "Run the same question with configured external providers."
            )

        input_tokens = len((system_prompt + " " + user_prompt).split())
        output_tokens = len(content.split())
        return ProviderResponse(
            content=content,
            resolved_model=self.config.model,
            request_id=f"mock-{digest[:20]}",
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            latency_ms=0,
            attempts=1,
            finish_reason="stop",
            metadata={"stage": stage, "mock": True},
        )

    def _jury_content(self, user_prompt: str) -> str:
        match = _LABELS_RE.search(user_prompt)
        labels: list[str] = []
        if match:
            try:
                decoded = json.loads(match.group(1))
            except json.JSONDecodeError:
                decoded = []
            if isinstance(decoded, list):
                labels = [
                    item
                    for item in decoded
                    if isinstance(item, str) and item
                ]
        if not labels:
            return json.dumps(
                {
                    "winner": None,
                    "ranking": [],
                    "confidence": "low",
                    "abstain": True,
                    "rationale": "No candidate labels were available.",
                    "material_disagreements": [],
                    "verification_needed": [
                        "Check the deterministic mock prompt format."
                    ],
                },
                separators=(",", ":"),
            )
        offset = self._seed % len(labels)
        ranking = labels[offset:] + labels[:offset]
        return json.dumps(
            {
                "winner": ranking[0],
                "ranking": ranking,
                "confidence": "low",
                "abstain": False,
                "rationale": (
                    "Candidates were ordered deterministically for an offline "
                    "execution-path test, not for factual quality."
                ),
                "material_disagreements": [],
                "verification_needed": [
                    "Use external providers for a substantive judgment."
                ],
            },
            separators=(",", ":"),
        )
