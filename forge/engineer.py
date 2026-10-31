"""
forge.engineer
=================
FORGE — Engenharia, Manutenção e Reparo Master (CORRIGIDO).

Pipeline (ver `forge.models`):
    DETECTED -> DIAGNOSING -> ROOT_CAUSE_FOUND -> FIX_PROPOSED
    -> RISK_ASSESSED -> [BLOCKED, quando necessário] -> FIX_APPROVED
    -> FIX_APPLYING -> FIX_APPLIED -> TESTING -> NON_REGRESSION_TESTING
    -> VALIDATED -> RELEASED  (ou ROLLED_BACK a qualquer momento)

CORREÇÃO (bloqueio persistido): `is_operation_blocked()` consulta o
`ForgeDiagnosticStore` (persistido quando `db_path` é informado) — não
mais um `set()` em memória que se perdia no restart. `find_blocked_operation`
sempre lê o estado FRESCO.

CORREÇÃO (BLOCKED não é estado morto): alcançável a partir de QUALQUER
etapa não-terminal (falha de teste, risco excessivo, decisão
preventiva) e RETOMÁVEL via `resume_from_block()` — sempre com
autorização explícita (Owner/ATLAS; risco ALTO exige o próprio Owner).

CORREÇÃO (apply_fix real): `execute_repair()` usa
`forge.repair.RepairExecutor` — plano de correção, checkpoint/backup,
dry-run, aplicação real, diff/evidência, rollback e tratamento de
falha parcial. Nunca é apenas uma mudança de estado.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.rbac import Principal, Role
from core.security.sanitize import sanitize_dict
from forge.models import (
    FORWARD_TRANSITIONS,
    RESUMABLE_FROM_BLOCKED,
    BlockClassification,
    Diagnostic,
    DiagnosticStatus,
    RiskLevel,
    Severity,
    _TERMINAL_STATUSES,
)
from forge.repair import FixPlan, RepairExecutor


class ForgeHierarchyViolation(Exception):
    pass


class InvalidPipelineTransition(Exception):
    pass


class FixNotAuthorized(Exception):
    """Levantado quando uma transição que exige autorização (aplicar
    correção, retomar de um bloqueio) não recebe autoridade suficiente
    — FORGE nunca avança sozinha."""


class DiagnosticNotFound(KeyError):
    pass


def _compute_backup_hash(backup: dict) -> str:
    """CORREÇÃO (item 2 — rollback real): hash estável do estado de
    backup, calculado no momento da correção e comparado no momento do
    rollback — detecta checkpoint corrompido/divergente ANTES de
    qualquer tentativa de restauração."""
    canonical = json.dumps(backup, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ForgeDiagnosticStore:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._diagnostics: dict[str, Diagnostic] = {}
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    def _row_to_diagnostic(self, row: sqlite3.Row) -> Diagnostic:
        keys = row.keys()
        return Diagnostic(
            diagnostic_id=row["diagnostic_id"], issue_summary=row["issue_summary"], root_cause=row["root_cause"],
            proposed_fix=row["proposed_fix"], risk_level=RiskLevel(row["risk_level"]),
            severity=Severity(row["severity"]) if "severity" in keys and row["severity"] else Severity.UNKNOWN,
            status=DiagnosticStatus(row["status"]), evidence=json.loads(row["evidence"]),
            blocked_operation=row["blocked_operation"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM forge_diagnostics").fetchall():
                d = self._row_to_diagnostic(row)
                self._diagnostics[d.diagnostic_id] = d

    def _persist(self, d: Diagnostic) -> None:
        self._diagnostics[d.diagnostic_id] = d
        if not self._db_path:
            return
        with db_module.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO forge_diagnostics "
                "(diagnostic_id, issue_summary, root_cause, proposed_fix, risk_level, status, evidence, "
                " blocked_operation, severity, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d.diagnostic_id, d.issue_summary, d.root_cause, d.proposed_fix, d.risk_level.value,
                 d.status.value, json.dumps(d.evidence, default=str), d.blocked_operation, d.severity.value,
                 d.created_at, d.updated_at),
            )

    def create(self, d: Diagnostic) -> Diagnostic:
        self._persist(d)
        return d

    def get(self, diagnostic_id: str) -> Diagnostic:
        """CORREÇÃO (restart): sempre lê o estado FRESCO do banco
        quando persistido — nunca confia só no cache em memória, que
        poderia estar obsoleto após um restart ou uma mudança feita por
        outra instância."""
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM forge_diagnostics WHERE diagnostic_id = ?", (diagnostic_id,)
                ).fetchone()
            if row is None:
                raise DiagnosticNotFound(diagnostic_id)
            d = self._row_to_diagnostic(row)
            self._diagnostics[diagnostic_id] = d
            return d
        if diagnostic_id not in self._diagnostics:
            raise DiagnosticNotFound(diagnostic_id)
        return self._diagnostics[diagnostic_id]

    def update(self, d: Diagnostic) -> Diagnostic:
        self._persist(d)
        return d

    def all(self) -> list:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM forge_diagnostics").fetchall()
            return [self._row_to_diagnostic(r) for r in rows]
        return list(self._diagnostics.values())

    def find_blocked_operation(self, operation_name: str) -> Diagnostic | None:
        """CORREÇÃO (bloqueio persistido): sempre a partir do estado
        FRESCO (`all()`, que já relê do banco quando persistido) —
        nunca de um `set()` em memória que se perderia no restart.

        CORREÇÃO (item 4.1): a operação continua BLOQUEADA durante
        TODO o pipeline de correção (BLOCKED -> ... -> VALIDATED) —
        não some assim que o diagnóstico sai do status BLOCKED
        literal. Só deixa de estar bloqueada em RELEASED/ROLLED_BACK
        (terminal) — `ROLLBACK_FAILED` mantém a operação bloqueada."""
        for d in self.all():
            if d.blocked_operation == operation_name and d.status not in _TERMINAL_STATUSES:
                return d
        return None


class ForgeEngineer:
    def __init__(self, *, principal: Principal, audit: AuditTrail, db_path: str | None = None) -> None:
        if principal.role != Role.FORGE:
            raise ForgeHierarchyViolation(
                f"ForgeEngineer exige um Principal de role FORGE — recebeu '{principal.role.value}'."
            )
        self.principal = principal
        self.audit = audit
        self.store = ForgeDiagnosticStore(db_path=db_path)

    # ------------------------------------------------------------------
    # Transições genéricas
    # ------------------------------------------------------------------

    def _advance(self, diagnostic_id: str, new_status: DiagnosticStatus, *, evidence: dict | None = None) -> Diagnostic:
        d = self.store.get(diagnostic_id)
        if d.status in _TERMINAL_STATUSES:
            raise InvalidPipelineTransition(
                f"diagnóstico '{diagnostic_id}' já está em estado terminal ({d.status.value})"
            )
        allowed = FORWARD_TRANSITIONS.get(d.status, set())
        if new_status not in allowed:
            raise InvalidPipelineTransition(
                f"não é possível ir de '{d.status.value}' para '{new_status.value}' — "
                f"transições permitidas a partir daqui: {[s.value for s in allowed]}"
            )
        if evidence is not None:
            d.evidence = evidence
        d.status = new_status
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action=f"forge_{new_status.value.lower()}", resource=diagnostic_id,
            outcome="SUCCESS", details={},
        )
        return d

    def _block(self, diagnostic_id: str, *, reason: str, evidence: dict | None = None) -> Diagnostic:
        """CORREÇÃO: BLOCKED alcançável a partir de QUALQUER estado
        não-terminal — nunca um beco sem saída."""
        d = self.store.get(diagnostic_id)
        if d.status in _TERMINAL_STATUSES:
            raise InvalidPipelineTransition(
                f"diagnóstico '{diagnostic_id}' já está em estado terminal ({d.status.value})"
            )
        d.evidence = {**d.evidence, **(evidence or {}), "block_reason": reason}
        d.status = DiagnosticStatus.BLOCKED
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_blocked", resource=diagnostic_id,
            outcome="SUCCESS", details={"reason": reason},
        )
        return d

    def resume_from_block(
        self, diagnostic_id: str, *, target_status: DiagnosticStatus, authorized_by: Principal,
    ) -> Diagnostic:
        """CORREÇÃO: BLOCKED nunca é estado morto — após correção
        AUTORIZADA, o pipeline continua. Autorização obrigatória
        (Owner/ATLAS; risco ALTO exige o próprio Owner) — nunca
        silencioso."""
        d = self.store.get(diagnostic_id)
        if d.status != DiagnosticStatus.BLOCKED:
            raise InvalidPipelineTransition(f"diagnóstico '{diagnostic_id}' não está BLOCKED")
        if target_status not in RESUMABLE_FROM_BLOCKED:
            raise InvalidPipelineTransition(
                f"não é possível retomar diretamente para '{target_status.value}' a partir de BLOCKED"
            )
        if authorized_by.role not in (Role.OWNER, Role.ATLAS):
            raise FixNotAuthorized(
                f"retomar de um bloqueio exige autorização de Owner ou ATLAS — '{authorized_by.role.value}' recusado"
            )
        if d.risk_level == RiskLevel.HIGH and authorized_by.role != Role.OWNER:
            raise FixNotAuthorized("retomar um bloqueio de risco ALTO exige o próprio Owner")
        d.evidence = {**d.evidence, "resumed_by": authorized_by.subject}
        d.status = target_status
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_resume_from_block", resource=diagnostic_id,
            outcome="SUCCESS", details={"target": target_status.value, "authorized_by": authorized_by.subject},
        )
        return d

    # ------------------------------------------------------------------
    # Pipeline — detecção até avaliação de risco
    # ------------------------------------------------------------------

    def detect(
        self, *, issue_summary: str, evidence: dict | None = None, severity: Severity = Severity.UNKNOWN,
    ) -> Diagnostic:
        d = Diagnostic(
            diagnostic_id=Diagnostic.new_id(), issue_summary=issue_summary,
            evidence=sanitize_dict(evidence or {}), severity=severity,
        )
        self.store.create(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_detect", resource=d.diagnostic_id,
            outcome="SUCCESS", details={"issue_summary": issue_summary, "severity": severity.value},
        )
        return d

    def diagnose(self, diagnostic_id: str, *, analysis: dict) -> Diagnostic:
        current = self.store.get(diagnostic_id)
        return self._advance(diagnostic_id, DiagnosticStatus.DIAGNOSING, evidence={**current.evidence, **sanitize_dict(analysis)})

    def identify_root_cause(self, diagnostic_id: str, *, root_cause: str, evidence: dict | None = None) -> Diagnostic:
        current = self.store.get(diagnostic_id)
        d = self._advance(
            diagnostic_id, DiagnosticStatus.ROOT_CAUSE_FOUND,
            evidence={**current.evidence, **sanitize_dict(evidence or {})},
        )
        d.root_cause = root_cause
        self.store.update(d)
        return d

    def propose_fix(self, diagnostic_id: str, *, proposed_fix: str) -> Diagnostic:
        d = self._advance(diagnostic_id, DiagnosticStatus.FIX_PROPOSED)
        d.proposed_fix = proposed_fix
        self.store.update(d)
        return d

    def assess_risk(self, diagnostic_id: str, *, risk_level: RiskLevel, evidence: dict | None = None) -> Diagnostic:
        current = self.store.get(diagnostic_id)
        d = self._advance(
            diagnostic_id, DiagnosticStatus.RISK_ASSESSED,
            evidence={**current.evidence, **sanitize_dict(evidence or {})},
        )
        d.risk_level = risk_level
        self.store.update(d)
        return d

    # ------------------------------------------------------------------
    # Aprovação e aplicação real da correção
    # ------------------------------------------------------------------

    def approve_fix(self, diagnostic_id: str, *, authorized_by: Principal) -> Diagnostic:
        d = self.store.get(diagnostic_id)
        if authorized_by.role not in (Role.OWNER, Role.ATLAS):
            raise FixNotAuthorized(
                f"aprovação de correção exige autorização de Owner ou ATLAS — '{authorized_by.role.value}' recusado"
            )
        if d.risk_level == RiskLevel.HIGH and authorized_by.role != Role.OWNER:
            raise FixNotAuthorized("correção de risco ALTO exige autorização do próprio Owner")
        applied = self._advance(diagnostic_id, DiagnosticStatus.FIX_APPROVED, evidence={**d.evidence, "authorized_by": authorized_by.subject})
        return applied

    def start_applying(self, diagnostic_id: str) -> Diagnostic:
        return self._advance(diagnostic_id, DiagnosticStatus.FIX_APPLYING)

    def execute_repair(
        self, diagnostic_id: str, *, plan: FixPlan, executor: RepairExecutor, dry_run: bool = False,
    ) -> Diagnostic:
        """CORREÇÃO — apply_fix real: exige `FIX_APPLYING` (chamar
        `start_applying()` antes), usa `RepairExecutor` de verdade
        (checkpoint, aplicação, diff, rollback automático em falha
        parcial). `dry_run=True` só registra a pré-visualização —
        nunca avança de estado."""
        d = self.store.get(diagnostic_id)
        if d.status != DiagnosticStatus.FIX_APPLYING:
            raise InvalidPipelineTransition(
                f"execute_repair exige o diagnóstico em FIX_APPLYING (chame start_applying() antes) — "
                f"estado atual: '{d.status.value}'"
            )
        if dry_run:
            result = executor.dry_run(plan)
            d.evidence = {**d.evidence, "dry_run_diff": result.diff}
            self.store.update(d)
            return d

        result = executor.apply(plan)
        if not result.applied:
            return self._block(diagnostic_id, reason=f"falha ao aplicar correção: {result.error}", evidence={"repair_error": result.error})
        # CORREÇÃO (rollback real): o backup capturado pelo
        # RepairExecutor é persistido na evidência do diagnóstico —
        # sobrevive a restart — junto de um HASH do backup
        # (`repair_backup_hash`), usado para detectar checkpoint
        # corrompido/divergente antes de tentar restaurar.
        backup_hash = _compute_backup_hash(result.backup)
        return self._advance(
            diagnostic_id, DiagnosticStatus.FIX_APPLIED,
            evidence={
                **d.evidence, "diff": result.diff, "applied_at": result.applied_at,
                "repair_backup": result.backup, "repair_backup_hash": backup_hash,
            },
        )

    # ------------------------------------------------------------------
    # Testes e não-regressão
    # ------------------------------------------------------------------

    def start_testing(self, diagnostic_id: str) -> Diagnostic:
        return self._advance(diagnostic_id, DiagnosticStatus.TESTING)

    def record_test_result(self, diagnostic_id: str, *, passed: bool, evidence: dict) -> Diagnostic:
        d = self.store.get(diagnostic_id)
        if d.status != DiagnosticStatus.TESTING:
            raise InvalidPipelineTransition(f"record_test_result exige TESTING — estado atual: '{d.status.value}'")
        merged = {**d.evidence, **sanitize_dict(evidence)}
        if not passed:
            return self._block(diagnostic_id, reason="testes falharam", evidence=merged)
        return self._advance(diagnostic_id, DiagnosticStatus.NON_REGRESSION_TESTING, evidence=merged)

    def record_non_regression_result(self, diagnostic_id: str, *, passed: bool, evidence: dict) -> Diagnostic:
        d = self.store.get(diagnostic_id)
        if d.status != DiagnosticStatus.NON_REGRESSION_TESTING:
            raise InvalidPipelineTransition(
                f"record_non_regression_result exige NON_REGRESSION_TESTING — estado atual: '{d.status.value}'"
            )
        merged = {**d.evidence, **sanitize_dict(evidence)}
        if not passed:
            return self._block(diagnostic_id, reason="testes de não-regressão falharam", evidence=merged)
        if not evidence:
            raise ValueError("validação exige evidência — nunca declarar sucesso sem prova")
        return self._advance(diagnostic_id, DiagnosticStatus.VALIDATED, evidence=merged)

    def release(self, diagnostic_id: str) -> Diagnostic:
        d = self._advance(diagnostic_id, DiagnosticStatus.RELEASED)
        return d

    def _fail_rollback(self, d: Diagnostic, *, reason: str, error: str) -> Diagnostic:
        d.evidence = {
            **d.evidence, "rollback_reason": reason, "rollback_restored": False, "rollback_error": error,
        }
        d.status = DiagnosticStatus.ROLLBACK_FAILED
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_rollback_failed", resource=d.diagnostic_id,
            outcome="ERROR", details={"error": error},
        )
        return d

    def rollback(self, diagnostic_id: str, *, reason: str, executor: RepairExecutor | None = None) -> Diagnostic:
        """CORREÇÃO (microcorreção finalíssima): `ROLLED_BACK` SÓ
        acontece quando o recurso é genuinamente restaurado e a
        restauração é VERIFICADA. Fluxo obrigatório:

            LOAD CHECKPOINT -> VALIDATE RESTORER -> RESTORE
            -> VERIFY RESTORATION -> COMPARE HASH/STATE
            -> RECORD EVIDENCE -> ROLLED_BACK

        Três desfechos claramente distintos (nunca confundidos):

        - CENÁRIO A — nenhuma correção real foi aplicada a este
          diagnóstico (nenhum `applied_at` no histórico): não há nada
          a desfazer. `status = NO_ROLLBACK_NEEDED` —
          `rollback_restored=False`, motivo registrado, NUNCA
          `ROLLED_BACK` (que declararia uma restauração que não
          aconteceu).
        - CENÁRIO B — uma correção FOI aplicada em algum momento
          (`applied_at` presente) mas o checkpoint está ausente ou
          corrompido agora: `status = ROLLBACK_FAILED` — a operação
          permanece bloqueada, erro registrado.
        - CENÁRIO C — checkpoint válido + restorer válido: restaura de
          verdade, verifica o estado restaurado contra o backup, só
          então `status = ROLLED_BACK`, `rollback_restored=True`."""
        d = self.store.get(diagnostic_id)
        if d.status in _TERMINAL_STATUSES:
            raise InvalidPipelineTransition(f"diagnóstico '{diagnostic_id}' já está em estado terminal ({d.status.value})")

        # LOAD CHECKPOINT
        backup = d.evidence.get("repair_backup")
        stored_hash = d.evidence.get("repair_backup_hash")
        fix_was_ever_applied = d.evidence.get("applied_at") is not None

        if not backup:
            if not fix_was_ever_applied:
                # CENÁRIO A: nenhuma correção real foi aplicada — nada
                # havia para desfazer. NUNCA ROLLED_BACK (que
                # declararia uma restauração que não ocorreu).
                d.evidence = {
                    **d.evidence, "rollback_reason": reason, "rollback_restored": False,
                    "rollback_note": "nenhuma correção real havia sido aplicada — nada a desfazer",
                }
                d.status = DiagnosticStatus.NO_ROLLBACK_NEEDED
                d.updated_at = time.time()
                self.store.update(d)
                self.audit.record(
                    actor=self.principal.subject, action="forge_no_rollback_needed", resource=diagnostic_id,
                    outcome="SUCCESS", details={"reason": reason},
                )
                return d
            # CENÁRIO B: uma correção FOI aplicada em algum momento,
            # mas o checkpoint que deveria existir sumiu — anomalia,
            # nunca tratada como "nada a fazer".
            return self._fail_rollback(
                d, reason=reason,
                error="checkpoint ausente apesar de uma correção ter sido aplicada anteriormente "
                "(applied_at presente, repair_backup ausente) — possível perda/corrupção de checkpoint",
            )

        # COMPARE HASH/STATE — checkpoint corrompido/divergente é
        # detectado ANTES de qualquer tentativa de restauração.
        if stored_hash is not None and _compute_backup_hash(backup) != stored_hash:
            return self._fail_rollback(
                d, reason=reason,
                error="checkpoint corrompido: hash do backup divergente do registrado no momento da correção",
            )

        # VALIDATE RESTORER — sem executor, ou executor incapaz de
        # cobrir TODOS os recursos do checkpoint, nunca finge sucesso.
        if executor is None:
            return self._fail_rollback(
                d, reason=reason,
                error="nenhum executor/restorer disponível para restaurar o recurso — reconecte o resource_store correto",
            )
        missing_permissions = [name for name in backup if name not in executor.allowed_resources]
        if missing_permissions:
            return self._fail_rollback(
                d, reason=reason,
                error=f"restorer não cobre todos os recursos do checkpoint (faltando: {missing_permissions})",
            )

        # RESTORE
        try:
            for name, old_value in backup.items():
                executor.resource_store[name] = old_value
        except Exception as exc:
            return self._fail_rollback(d, reason=reason, error=f"falha ao restaurar: {exc}")

        # VERIFY RESTORATION
        try:
            restored_ok = all(executor.resource_store.get(name) == old_value for name, old_value in backup.items())
        except Exception as exc:
            return self._fail_rollback(d, reason=reason, error=f"falha ao verificar restauração: {exc}")
        if not restored_ok:
            return self._fail_rollback(d, reason=reason, error="estado pós-restauração não confere com o backup")

        # RECORD EVIDENCE -> ROLLED_BACK
        d.evidence = {
            **d.evidence, "rollback_reason": reason, "rollback_restored": True, "rollback_verified_state": backup,
        }
        d.status = DiagnosticStatus.ROLLED_BACK
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_rolled_back", resource=diagnostic_id,
            outcome="SUCCESS", details={"reason": reason, "restored": True},
        )
        return d

    # ------------------------------------------------------------------
    # Bloqueio preventivo de operação (persistido — CORREÇÃO)
    # ------------------------------------------------------------------

    def block_operation(
        self, *, operation_name: str, reason: str, evidence: dict | None = None, severity: Severity = Severity.HIGH,
        classification: BlockClassification = BlockClassification.TECHNICAL,
    ) -> Diagnostic:
        """CORREÇÃO (item 4.1): `classification` decide como o bloqueio
        pode ser liberado depois. `TECHNICAL` (padrão — mais seguro)
        exige o pipeline de correção completo até `VALIDATED` antes de
        `release_operation_block` aceitar liberar. Só
        `ADMINISTRATIVE` (bloqueios não-técnicos — ex: pausa
        operacional deliberada) permite o fluxo simplificado
        (autorização + justificativa)."""
        d = self.detect(issue_summary=f"bloqueio preventivo: {operation_name}", evidence=evidence, severity=severity)
        d.blocked_operation = operation_name
        d.evidence = {**d.evidence, "block_reason": reason, "block_classification": classification.value}
        d.status = DiagnosticStatus.BLOCKED
        d.updated_at = time.time()
        self.store.update(d)
        self.audit.record(
            actor=self.principal.subject, action="forge_block_operation", resource=operation_name,
            outcome="SUCCESS", details={"reason": reason, "classification": classification.value},
        )
        return d

    def is_operation_blocked(self, operation_name: str) -> bool:
        """CORREÇÃO (bloqueio persistido): consulta o store (persistido
        quando `db_path` é informado) — nunca um `set()` em memória que
        se perdia no restart."""
        return self.store.find_blocked_operation(operation_name) is not None

    def release_operation_block(
        self, diagnostic_id: str, *, authorized_by: Principal, justification: str | None = None,
    ) -> Diagnostic:
        """CORREÇÃO (item 4.1): bloqueios TECHNICAL só liberam depois
        do pipeline de correção completo alcançar `VALIDATED` (fix
        aplicado + testado + não-regressão + validado) — autorização
        sozinha NUNCA é suficiente para um bloqueio técnico. Sem isso:
        `InvalidPipelineTransition` ("RELEASE DENIED"). Bloqueios
        ADMINISTRATIVE continuam usando o fluxo simplificado, mas
        exigem `justification` explícita (auditada)."""
        if authorized_by.role not in (Role.OWNER, Role.ATLAS):
            raise FixNotAuthorized(
                f"liberar um bloqueio de operação exige autorização de Owner ou ATLAS — "
                f"'{authorized_by.role.value}' recusado"
            )
        d = self.store.get(diagnostic_id)
        classification = d.evidence.get("block_classification", BlockClassification.TECHNICAL.value)

        if classification == BlockClassification.ADMINISTRATIVE.value:
            if not justification:
                raise ValueError(
                    "liberação de bloqueio ADMINISTRATIVE exige justificativa explícita — nenhum bypass silencioso"
                )
            if d.status in _TERMINAL_STATUSES:
                raise InvalidPipelineTransition(f"diagnóstico '{diagnostic_id}' já está em estado terminal ({d.status.value})")
            d.evidence = {**d.evidence, "unblocked_by": authorized_by.subject, "unblock_justification": justification}
            d.status = DiagnosticStatus.RELEASED
            d.updated_at = time.time()
            self.store.update(d)
            self.audit.record(
                actor=self.principal.subject, action="forge_release_operation_block", resource=d.blocked_operation or diagnostic_id,
                outcome="SUCCESS", details={"authorized_by": authorized_by.subject, "bypass": "administrative", "justification": justification},
            )
            return d

        # TECHNICAL — RELEASE DENIED sem o pipeline de correção completo.
        if d.status != DiagnosticStatus.VALIDATED:
            raise InvalidPipelineTransition(
                f"RELEASE DENIED: bloqueio técnico só pode ser liberado após o pipeline de correção completo "
                f"(fix aplicado + testado + não-regressão + validado) — estado atual: '{d.status.value}', "
                "exigido: 'VALIDATED'"
            )
        d.evidence = {**d.evidence, "unblocked_by": authorized_by.subject}
        released = self.release(diagnostic_id)
        return released
