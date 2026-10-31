"""
commercial.learning_loop
==========================
Learning Loop (Seção 52). Aprende separadamente por canal, plataforma,
produto, oferta, funil, formato, campanha. Registra o que funcionou,
o que falhou, quando, onde, para qual público, resultado, limitações.
Não transforma correlação em certeza automaticamente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

MIN_OBSERVATIONS_FOR_PATTERN = 5


@dataclass
class LearningRecord:
    scope_key: str  # ex: "channel:abc|product:xyz|format:short"
    outcome: str     # "worked" | "failed" | "inconclusive"
    context: dict
    limitations: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class LearningPattern:
    scope_key: str
    observations: int
    worked_count: int
    failed_count: int
    confidence: str  # "LOW" | "MEDIUM" — nunca "CERTAIN" (correlação != certeza)


class LearningLoop:
    def __init__(self) -> None:
        self._records: list[LearningRecord] = []

    def record(self, *, scope_key: str, outcome: str, context: dict, limitations: str = "") -> LearningRecord:
        if outcome not in ("worked", "failed", "inconclusive"):
            raise ValueError("outcome deve ser 'worked', 'failed' ou 'inconclusive'")
        record = LearningRecord(scope_key=scope_key, outcome=outcome, context=context, limitations=limitations)
        self._records.append(record)
        return record

    def pattern_for(self, scope_key: str) -> LearningPattern:
        matching = [r for r in self._records if r.scope_key == scope_key]
        worked = sum(1 for r in matching if r.outcome == "worked")
        failed = sum(1 for r in matching if r.outcome == "failed")

        confidence = "LOW"
        if len(matching) >= MIN_OBSERVATIONS_FOR_PATTERN:
            # Mesmo com volume razoável, o teto é MEDIUM: correlação não
            # vira certeza automaticamente (regra explícita da Seção 52).
            confidence = "MEDIUM"

        return LearningPattern(
            scope_key=scope_key,
            observations=len(matching),
            worked_count=worked,
            failed_count=failed,
            confidence=confidence,
        )

    def all_records(self) -> list[LearningRecord]:
        return list(self._records)
