"""
core.evidence.claim_registry
================================
CORREÇÃO (item 10 — Claim Registry / Claim Binding): `related_claim_id`
existia em `EvidenceRecord` desde a rodada anterior, mas nada
resolvia/validava esse vínculo de verdade. `ClaimRegistry` é a fonte
de verdade — todo `claim_id` citado por uma `EvidenceRecord`/
`MetricEvidence` precisa RESOLVER para um `ClaimRecord` real aqui, e
a `strategy_id`/`related_metric_names` do claim precisam bater com o
que está sendo avaliado (item 11 — binding completo).

BLOCO FINAL 3 (fechamento da V1 — persistência restante do
ClaimRegistry): até esta rodada, `ClaimRegistry` era o ÚNICO dos
quatro registries de `ApplicationGovernanceContext` (autoridade/
verifier/source/claim) sem `db_path` — `restore_for_app()` já
restaurava autoridade/verifier/source corretamente após um restart,
mas recriava um `ClaimRegistry()` vazio, perdendo todo claim
registrado antes do restart (e, com ele, a capacidade de
`related_claim_id`/`matches()` resolverem evidências antigas). Mesma
disciplina agora aplicada aqui: `db_path` opcional, `INSERT OR
REPLACE` em `register()`, `_load_persisted_claims()` na construção.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class ClaimRecord:
    claim_id: str
    strategy_id: str
    claim_type: str
    statement: str
    related_metric_names: list = field(default_factory=list)
    status: str = "OPEN"
    evidence_requirements: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"claim_{uuid.uuid4().hex[:12]}"


class ClaimRegistry:
    """VAZIO por padrão — nenhum `claim_id` resolve até ser
    genuinamente registrado.

    BLOCO FINAL 3: com `db_path` fornecido, todo `register()` é
    persistido de imediato e recarregado na construção seguinte —
    sobrevive a restart na mesma disciplina de `VerifierRegistry`/
    `SourceRegistry`/`AuthorityRegistry`.

    MICROCORREÇÃO BF3 (isolamento multi-tenant real — falha real
    reproduzida por auditoria independente: usando o mesmo `db_path`,
    um appB/tenantB conseguia `restore_for_app()` e resolver claims
    registrados por um appA/tenantA): `application_id`/`tenant_id`/
    `governance_context_id` agora são recebidos AQUI, no construtor —
    ANTES de qualquer carga acontecer — e usados em TODO `SELECT`/
    `INSERT` contra `evidence_claim_registry`. Nunca setados DEPOIS
    da carga (o que deixaria uma janela onde `_load_persisted_claims()`
    já rodou sem filtro algum, carregando claims de QUALQUER tenant
    para a memória)."""

    def __init__(
        self, *, db_path: str | None = None, application_id: str = "default", tenant_id: str = "default",
        governance_context_id: str = "",
    ) -> None:
        self._claims: dict = {}
        self.db_path = db_path
        # CORREÇÃO CRÍTICA: escopo fixado ANTES de `_load_persisted_
        # claims()` ser chamado logo abaixo — nunca depois.
        self.application_id = application_id
        self.tenant_id = tenant_id
        self.governance_context_id = governance_context_id
        if db_path:
            self._load_persisted_claims()

    def _load_persisted_claims(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_claim_registry WHERE application_id = ? AND tenant_id = ? "
                "AND governance_context_id = ?",
                (self.application_id, self.tenant_id, self.governance_context_id),
            ).fetchall()
        for row in rows:
            record = ClaimRecord(
                claim_id=row["claim_id"], strategy_id=row["strategy_id"], claim_type=row["claim_type"],
                statement=row["statement"], related_metric_names=json.loads(row["related_metric_names"]),
                status=row["status"], evidence_requirements=json.loads(row["evidence_requirements"]),
                created_at=row["created_at"],
            )
            self._claims[record.claim_id] = record

    def _persist_claim(self, record: ClaimRecord) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence_claim_registry "
                "(claim_id, strategy_id, claim_type, statement, related_metric_names, status, "
                "evidence_requirements, created_at, application_id, tenant_id, governance_context_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.claim_id, record.strategy_id, record.claim_type, record.statement,
                    json.dumps(record.related_metric_names), record.status,
                    json.dumps(record.evidence_requirements), record.created_at,
                    self.application_id, self.tenant_id, self.governance_context_id,
                ),
            )

    def register(self, record: ClaimRecord) -> ClaimRecord:
        self._claims[record.claim_id] = record
        self._persist_claim(record)
        return record

    def resolve(self, claim_id: str) -> ClaimRecord | None:
        return self._claims.get(claim_id)

    def matches(self, claim_id: str, *, strategy_id: str | None = None, metric_name: str | None = None) -> bool:
        """CORREÇÃO (item 11/12 — binding completo): confirma que o
        claim resolvido genuinamente pertence à `strategy_id` e (se
        informado) referencia a `metric_name` — usado com
        canonicalização pelo chamador."""
        record = self.resolve(claim_id)
        if record is None:
            return False
        if strategy_id is not None and record.strategy_id != strategy_id:
            return False
        if metric_name is not None and record.related_metric_names:
            from core.evidence.registry import CanonicalMetricRegistry

            canonical_requested = CanonicalMetricRegistry.canonicalize(metric_name)
            canonical_bound = {CanonicalMetricRegistry.canonicalize(m) for m in record.related_metric_names}
            if canonical_requested not in canonical_bound:
                return False
        return True

    def all(self) -> list:
        return list(self._claims.values())
