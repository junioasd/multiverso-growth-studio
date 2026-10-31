"""
radar_web.evolution
=======================
RADAR WEB COMERCIAL — VISÃO EVOLUTIVA (itens 11-14, 18) +
CORREÇÃO (item 4 — identidade estável e versionamento longitudinal).

Ciclo: OBSERVAR -> COMPARAR -> DETECTAR MUDANÇA -> ANALISAR -> TESTAR
-> VALIDAR -> ATUALIZAR -> APRENDER -> EVOLUIR.

`StrategyHistoryStore` é estritamente APPEND-ONLY — nunca sobrescreve
uma entrada existente, cada revisão gera uma nova versão/linha.

CORREÇÃO (item 4): `strategy_id` agora vem de
`radar_web.models.compute_strategy_fingerprint()` — um hash
ESTRUTURAL estável (não mais UUID aleatório) — a MESMA estratégia
observada em anos diferentes SEMPRE recebe o MESMO `strategy_id`.
Além disso, 3 conceitos de versão antes colapsados em um só campo
`version` ficam EXPLICITAMENTE separados:

    strategy_version        — contador GLOBAL da estratégia (cresce a
                               cada revisão, em QUALQUER ano)
    annual_snapshot_version — a Nª revisão DENTRO do MESMO ano (1 na
                               primeira revisão do ano)
    revision_number         — igual a `annual_snapshot_version`,
                               exposto com nome explícito por clareza

Uma segunda revisão no MESMO ano para a MESMA estratégia SEMPRE exige
`revision_reason` explícito — nunca um `[1, 1]` silencioso.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from core.models import db as db_module
from radar_web.engine import RadarWebComercial
from radar_web.models import FunnelMaturity


class DuplicateAnnualReviewError(Exception):
    """Levantado quando uma segunda revisão no MESMO ano, para a MESMA
    estratégia, é solicitada sem `revision_reason` explícito."""


@dataclass
class StrategyHistoryEntry:
    entry_id: str
    strategy_id: str
    version: int  # alias histórico de `strategy_version` — mantido por compatibilidade
    year: int
    status: FunnelMaturity
    niches: list = field(default_factory=list)
    channels: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)
    score: float | None = None
    confidence: float = 0.0
    changes: str = ""
    outcome: str | None = None
    notes: str = ""
    created_at: float = field(default_factory=time.time)
    strategy_version: int = 1
    annual_snapshot_version: int = 1
    revision_number: int = 1
    revision_reason: str | None = None

    def __post_init__(self) -> None:
        # `version` e `strategy_version` são o MESMO conceito — mantém
        # os dois sincronizados independente de qual foi passado.
        if self.strategy_version == 1 and self.version != 1:
            self.strategy_version = self.version
        else:
            self.version = self.strategy_version
        if self.revision_number == 1 and self.annual_snapshot_version != 1:
            self.revision_number = self.annual_snapshot_version
        else:
            self.annual_snapshot_version = self.revision_number

    @staticmethod
    def new_id() -> str:
        return f"strhist_{uuid.uuid4().hex[:12]}"


class StrategyHistoryStore:
    """APPEND-ONLY — `add_entry()` NUNCA sobrescreve uma entrada
    existente; cada chamada cria uma linha nova. Persistência opcional
    via `db_path` (mesmo padrão já estabelecido em todo o sistema)."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self._entries: list = []
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _row_to_entry(self, row: sqlite3.Row) -> StrategyHistoryEntry:
        keys = row.keys()
        return StrategyHistoryEntry(
            entry_id=row["entry_id"], strategy_id=row["strategy_id"], version=row["version"],
            year=row["year"], status=FunnelMaturity(row["status"]), niches=json.loads(row["niches"]),
            channels=json.loads(row["channels"]), evidence_refs=json.loads(row["evidence_refs"]),
            score=row["score"], confidence=row["confidence"], changes=row["changes"],
            outcome=row["outcome"], notes=row["notes"], created_at=row["created_at"],
            strategy_version=row["strategy_version"] if "strategy_version" in keys else row["version"],
            annual_snapshot_version=row["annual_snapshot_version"] if "annual_snapshot_version" in keys else 1,
            revision_number=row["revision_number"] if "revision_number" in keys else 1,
            revision_reason=row["revision_reason"] if "revision_reason" in keys else None,
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM strategy_history").fetchall():
                self._entries.append(self._row_to_entry(row))

    def add_entry(self, entry: StrategyHistoryEntry) -> StrategyHistoryEntry:
        self._entries.append(entry)
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO strategy_history "
                    "(entry_id, strategy_id, version, year, status, niches, channels, evidence_refs, "
                    " score, confidence, changes, outcome, notes, created_at, strategy_version, "
                    " annual_snapshot_version, revision_number, revision_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entry.entry_id, entry.strategy_id, entry.version, entry.year, entry.status.value,
                     json.dumps(entry.niches, default=str), json.dumps(entry.channels, default=str),
                     json.dumps(entry.evidence_refs, default=str), entry.score, entry.confidence,
                     entry.changes, entry.outcome, entry.notes, entry.created_at, entry.strategy_version,
                     entry.annual_snapshot_version, entry.revision_number, entry.revision_reason),
                )
        return entry

    def all(self) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM strategy_history").fetchall()
            return [self._row_to_entry(r) for r in rows]
        return list(self._entries)

    def history_for(self, strategy_id: str) -> list:
        """Ordenado por `strategy_version` (contador global) — permite
        reconstruir a linha do tempo completa de uma estratégia."""
        entries = [e for e in self.all() if e.strategy_id == strategy_id]
        return sorted(entries, key=lambda e: e.strategy_version)

    def by_year(self, year: int) -> list:
        return [e for e in self.all() if e.year == year]

    def latest_version(self, strategy_id: str) -> StrategyHistoryEntry | None:
        history = self.history_for(strategy_id)
        return history[-1] if history else None

    def next_versions_for(self, strategy_id: str, year: int) -> tuple:
        """CORREÇÃO (item 4): calcula `strategy_version` (global) e
        `annual_snapshot_version` (dentro do ano) separadamente —
        nunca reutiliza `[1, 1]` silenciosamente para uma segunda
        revisão do mesmo ano."""
        history = self.history_for(strategy_id)
        next_strategy_version = (history[-1].strategy_version + 1) if history else 1
        same_year_entries = [e for e in history if e.year == year]
        next_annual_snapshot_version = len(same_year_entries) + 1
        return next_strategy_version, next_annual_snapshot_version

    def compare_years(self, strategy_id: str, year_a: int, year_b: int) -> dict:
        """Comparação longitudinal — item 18: saber o que funcionava,
        quando, onde, quando começou a cair, quando voltou."""
        entry_a = next((e for e in self.history_for(strategy_id) if e.year == year_a), None)
        entry_b = next((e for e in self.history_for(strategy_id) if e.year == year_b), None)
        return {
            "strategy_id": strategy_id, "year_a": year_a, "year_b": year_b,
            "status_a": entry_a.status.value if entry_a else None,
            "status_b": entry_b.status.value if entry_b else None,
            "confidence_a": entry_a.confidence if entry_a else None,
            "confidence_b": entry_b.confidence if entry_b else None,
            "changed": (entry_a.status if entry_a else None) != (entry_b.status if entry_b else None),
        }


class AnnualReviewEngine:
    """Item 12 — revisão estratégica anual obrigatória. Ciclo:
    OBSERVAR (discover) -> COMPARAR (com o ano anterior) -> DETECTAR
    MUDANÇA -> registra nova versão -> nunca apaga histórico.

    CORREÇÃO (item 4): uma segunda chamada para o MESMO `year` e a
    MESMA estratégia SEMPRE exige `revision_reason` explícito —
    `DuplicateAnnualReviewError` se ausente. `strategy_id` estável
    (via `RadarWebComercial`/`MockStrategySource` corrigidos) garante
    que a MESMA estratégia em anos diferentes sempre compara
    corretamente."""

    def __init__(self, *, radar: RadarWebComercial, history: StrategyHistoryStore) -> None:
        self.radar = radar
        self.history = history

    def run_annual_review(self, *, year: int, niche: str | None = None, revision_reason: str | None = None) -> dict:
        discovered = self.radar.discover(niche=niche)

        new_entries = []
        for strategy in discovered:
            classified = self.radar.classify_maturity(strategy)
            history = self.history.history_for(classified.strategy_id)
            same_year_entries = [e for e in history if e.year == year]

            if same_year_entries and not revision_reason:
                raise DuplicateAnnualReviewError(
                    f"revisão repetida no mesmo ano ({year}) para '{classified.strategy_id}' exige "
                    "`revision_reason` explícito — nunca uma segunda revisão silenciosa"
                )

            next_strategy_version, next_annual_snapshot_version = self.history.next_versions_for(
                classified.strategy_id, year,
            )
            prior = history[-1] if history else None
            change_note = (
                f"status anterior ({prior.year}, v{prior.strategy_version}): {prior.status.value} -> "
                f"status atual ({year}, v{next_strategy_version}): {classified.maturity.value}"
                if prior else f"nova estratégia observada em {year}: {classified.maturity.value}"
            )
            entry = StrategyHistoryEntry(
                entry_id=StrategyHistoryEntry.new_id(), strategy_id=classified.strategy_id,
                version=next_strategy_version, year=year, status=classified.maturity, niches=[classified.niche],
                channels=[classified.channel], evidence_refs=list(classified.evidence_ids),
                confidence=classified.confidence, changes=change_note, notes="",
                strategy_version=next_strategy_version, annual_snapshot_version=next_annual_snapshot_version,
                revision_number=next_annual_snapshot_version,
                revision_reason=revision_reason if same_year_entries else None,
            )
            self.history.add_entry(entry)
            new_entries.append(entry)

        return {
            "year": year, "discovered": len(discovered), "entries_recorded": len(new_entries),
            "entries": new_entries,
        }
