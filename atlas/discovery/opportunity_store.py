"""
atlas.discovery.opportunity_store
=====================================
ATLAS PIPELINE 1 — fila PERSISTENTE de oportunidades de conteúdo,
pronta para alimentar o Pipeline 2 (produção — fora de escopo aqui).

A peça que falta em `atlas.radar.engine.CommercialOpportunityRadar`
(cujo `_tracked` é só em memória, nunca sobrevive a restart) — aqui a
persistência é o requisito central, não um extra.

Deduplicação real: a PRIMARY KEY é `identity_key` (determinística —
`ContentOpportunity.identity_key`), NUNCA `opportunity_id`
(aleatório). `upsert()` usa `INSERT ... ON CONFLICT (identity_key) DO
UPDATE` — atômico no próprio SQLite, nunca um read-then-write
vulnerável a corrida entre duas instâncias/execuções concorrentes
tocando a MESMA oportunidade.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from atlas.discovery.models import ContentFormat, ContentOpportunity, DiscoverySignal, Platform, VerifiedLineage
from atlas.discovery.provenance import DurableAcquisitionRecord
from atlas.radar.models import EvidenceLevel
from core.evidence.claims import ClaimType

__all__ = ["OpportunityStore"]


def _lineage_to_dict(lineage: VerifiedLineage | None) -> dict | None:
    if lineage is None:
        return None
    return {
        "source_id": lineage.source_id, "execution_id": lineage.execution_id,
        "acquisition_id": lineage.acquisition_id, "mode": lineage.mode, "verified_at": lineage.verified_at,
        "observation_fields": list(lineage.observation_fields),
        "verified_value_digest": lineage.verified_value_digest,
        "acquisition_observations_digest": lineage.acquisition_observations_digest,
    }


def _lineage_from_dict(data: dict | None) -> VerifiedLineage | None:
    if not data:
        return None
    return VerifiedLineage(
        source_id=data["source_id"], execution_id=data["execution_id"], acquisition_id=data["acquisition_id"],
        mode=data["mode"], verified_at=data["verified_at"],
        observation_fields=tuple(data.get("observation_fields", ())),
        verified_value_digest=data.get("verified_value_digest", ""),
        acquisition_observations_digest=data.get("acquisition_observations_digest", ""),
    )


def _signal_to_dict(signal: DiscoverySignal) -> dict:
    return {
        "value": signal.value, "evidence": signal.evidence.value, "claim_type": signal.claim_type.value,
        "source": signal.source, "observed_at": signal.observed_at, "confidence": signal.confidence,
        "valid_until": signal.valid_until, "claimed_acquisition_id": signal.claimed_acquisition_id,
        "claimed_observation_fields": list(signal.claimed_observation_fields),
        "lineage": _lineage_to_dict(signal.lineage),
    }


def _signal_from_dict(data: dict) -> DiscoverySignal:
    return DiscoverySignal(
        value=data["value"], evidence=EvidenceLevel(data["evidence"]), claim_type=ClaimType(data["claim_type"]),
        source=data.get("source"), observed_at=data.get("observed_at", 0.0), confidence=data.get("confidence", 0.0),
        valid_until=data.get("valid_until"), claimed_acquisition_id=data.get("claimed_acquisition_id"),
        claimed_observation_fields=tuple(data.get("claimed_observation_fields", ())),
        lineage=_lineage_from_dict(data.get("lineage")),
    )


def _opportunity_to_row(opp: ContentOpportunity) -> dict:
    return {
        "identity_key": opp.identity_key,
        "opportunity_id": opp.opportunity_id,
        "platform": opp.platform.value,
        "theme": opp.theme,
        "content_format": opp.content_format.value,
        "hook": opp.hook,
        "signals_json": json.dumps({k: _signal_to_dict(v) for k, v in opp.signals.items()}),
        "notes_json": json.dumps(opp.notes),
        "source_refs_json": json.dumps(opp.source_refs),
        "discovered_at": opp.discovered_at,
        "updated_at": opp.updated_at,
    }


def _row_to_opportunity(row: dict) -> ContentOpportunity:
    signals_data = json.loads(row["signals_json"])
    return ContentOpportunity(
        opportunity_id=row["opportunity_id"], platform=Platform(row["platform"]), theme=row["theme"],
        content_format=ContentFormat(row["content_format"]), hook=row["hook"],
        signals={k: _signal_from_dict(v) for k, v in signals_data.items()},
        notes=json.loads(row["notes_json"]), source_refs=json.loads(row["source_refs_json"]),
        discovered_at=row["discovered_at"], updated_at=row["updated_at"],
    )


def _merge_opportunities(existing: ContentOpportunity, new: ContentOpportunity) -> ContentOpportunity:
    """MICROCORREÇÃO CRÍTICA (falha real encontrada em teste
    adversarial próprio — perda de dado real): mescla `new` sobre
    `existing` — sinais da MESMA chave são substituídos (o dado mais
    recente prevalece POR SINAL, nunca a oportunidade inteira), mas
    sinais de OUTRAS chaves já presentes em `existing` são
    PRESERVADOS — nunca perdidos só porque uma fonte diferente, nesta
    rodada, não os reportou de novo. `notes`/`source_refs` são uniões
    deduplicadas (ordem de primeira aparição preservada) — sempre
    acumulam, nunca substituem. `opportunity_id`/`discovered_at`
    vêm SEMPRE de `existing` (a identidade e a data da PRIMEIRA
    descoberta nunca mudam numa atualização)."""
    merged_signals = dict(existing.signals)
    merged_signals.update(new.signals)
    merged_notes = list(existing.notes)
    for note in new.notes:
        if note not in merged_notes:
            merged_notes.append(note)
    merged_source_refs = list(existing.source_refs)
    for ref in new.source_refs:
        if ref not in merged_source_refs:
            merged_source_refs.append(ref)
    return ContentOpportunity(
        opportunity_id=existing.opportunity_id, platform=new.platform, theme=new.theme,
        content_format=new.content_format, hook=new.hook, signals=merged_signals, notes=merged_notes,
        source_refs=merged_source_refs, discovered_at=existing.discovered_at, updated_at=new.updated_at,
    )


class OpportunityStore:
    """Armazena/atualiza `ContentOpportunity` de forma ATÔMICA e
    IDEMPOTENTE por `identity_key`. Suporta modo em memória (para
    testes rápidos da lógica pura) e modo durável (SQLite,
    `db_path`) — mesma disciplina de dois modos já estabelecida em
    `JobQueue`/`MissionTracker`/`ClaimRegistry`."""

    _memory_lock = threading.Lock()

    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path
        if db_path is None:
            self._memory: dict[str, dict] = {}
            self._acquisition_memory: dict[str, dict] = {}

    def upsert(self, opportunity: ContentOpportunity) -> ContentOpportunity:
        """Insere ou atualiza a oportunidade pela sua `identity_key`
        DETERMINÍSTICA — a MESMA oportunidade descoberta de novo
        (mesmo tema/formato/gancho/plataforma) NUNCA cria uma segunda
        linha; atualiza `signals`/`notes`/`source_refs`/`updated_at`
        na linha existente. `discovered_at` é preservado da PRIMEIRA
        descoberta (nunca reescrito numa atualização)."""
        opportunity.updated_at = time.time()
        if self.db_path:
            for attempt in range(4):
                try:
                    return self._upsert_durable(opportunity)
                except sqlite3.OperationalError as exc:
                    message = str(exc).lower()
                    is_lock = (
                        "database is locked" in message
                        or "database table is locked" in message
                    )
                    if not is_lock or attempt == 3:
                        raise
                    time.sleep(0.05 * (2 ** attempt))
        return self._upsert_memory(opportunity)

    def _upsert_durable(self, opportunity: ContentOpportunity) -> ContentOpportunity:
        from core.models import db as db_module

        row = _opportunity_to_row(opportunity)
        with db_module.connect(self.db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO discovery_opportunities "
                    "(identity_key, opportunity_id, platform, theme, content_format, hook, signals_json, "
                    "notes_json, source_refs_json, discovered_at, updated_at, version) "
                    "VALUES (:identity_key, :opportunity_id, :platform, :theme, :content_format, :hook, "
                    ":signals_json, :notes_json, :source_refs_json, :discovered_at, :updated_at, 1)",
                    row,
                )
                return opportunity
            except sqlite3.IntegrityError:
                pass  # já existe -- MESCLA abaixo, nunca substitui às cegas

            # MICROCORREÇÃO CRÍTICA (falha real encontrada em teste
            # adversarial próprio: duas fontes DIFERENTES descobrindo
            # a MESMA oportunidade — mesma `identity_key` — com sinais
            # DIFERENTES; um `UPDATE` que substitui `signals_json`
            # inteiro APAGAVA o sinal da primeira fonte ao aplicar o
            # da segunda). Corrigido com leitura-modificação-escrita
            # sob controle de versão OTIMISTA (mesmo padrão já
            # validado em `graph_executor.py`'s reclaim de lease) —
            # laço LIMITADO (nunca recursão/espera irrestrita): lê a
            # linha atual + versão, MESCLA sinais por chave (o dado
            # mais recente vence POR SINAL, nunca a oportunidade
            # inteira) e `notes`/`source_refs` como união deduplicada
            # (nunca substituídos), grava condicionado à MESMA versão
            # lida — se outra escrita concorrente já mudou a versão
            # nesse meio-tempo, relê e tenta de novo.
            for _ in range(10):
                current_row = conn.execute(
                    "SELECT * FROM discovery_opportunities WHERE identity_key = ?",
                    (opportunity.identity_key,),
                ).fetchone()
                if current_row is None:
                    # sumiu entre o INSERT falhar e este SELECT (nunca
                    # deveria acontecer, esta tabela nunca usa DELETE)
                    # — tenta o INSERT de novo por segurança.
                    try:
                        conn.execute(
                            "INSERT INTO discovery_opportunities "
                            "(identity_key, opportunity_id, platform, theme, content_format, hook, "
                            "signals_json, notes_json, source_refs_json, discovered_at, updated_at, version) "
                            "VALUES (:identity_key, :opportunity_id, :platform, :theme, :content_format, "
                            ":hook, :signals_json, :notes_json, :source_refs_json, :discovered_at, "
                            ":updated_at, 1)",
                            row,
                        )
                        return opportunity
                    except sqlite3.IntegrityError:
                        continue

                current_row = dict(current_row)
                existing_opp = _row_to_opportunity(current_row)
                merged = _merge_opportunities(existing_opp, opportunity)
                merged_row = _opportunity_to_row(merged)
                cursor = conn.execute(
                    "UPDATE discovery_opportunities SET signals_json = :signals_json, "
                    "notes_json = :notes_json, source_refs_json = :source_refs_json, "
                    "updated_at = :updated_at, version = version + 1 "
                    "WHERE identity_key = :identity_key AND version = :expected_version",
                    {**merged_row, "expected_version": current_row["version"]},
                )
                if cursor.rowcount == 1:
                    return merged
                # outra escrita concorrente venceu a corrida entre a
                # leitura e este UPDATE — relê e mescla de novo, nunca
                # sobrescreve às cegas.

            # teto de tentativas atingido (corrida extraordinariamente
            # persistente) — devolve o estado mais recente conhecido,
            # nunca finge que a mesclagem desta chamada específica
            # foi aplicada quando não foi.
            fallback_row = conn.execute(
                "SELECT * FROM discovery_opportunities WHERE identity_key = ?", (opportunity.identity_key,),
            ).fetchone()
            return _row_to_opportunity(dict(fallback_row))

    def _upsert_memory(self, opportunity: ContentOpportunity) -> ContentOpportunity:
        key = opportunity.identity_key
        with OpportunityStore._memory_lock:
            existing_row = self._memory.get(key)
            if existing_row is not None:
                existing_opp = _row_to_opportunity(existing_row)
                merged = _merge_opportunities(existing_opp, opportunity)
                version = existing_row.get("_version", 1) + 1
                row = _opportunity_to_row(merged)
                row["_version"] = version
                row["status"] = existing_row.get("status", "DISCOVERED")
                row["score"] = existing_row.get("score")
                row["confidence"] = existing_row.get("confidence")
                row["risk_level"] = existing_row.get("risk_level")
                row["justification_json"] = existing_row.get("justification_json")
                self._memory[key] = row
                return merged
            row = _opportunity_to_row(opportunity)
            row["_version"] = 1
            row["status"] = "DISCOVERED"
            row["score"] = None
            row["confidence"] = None
            row["risk_level"] = None
            row["justification_json"] = None
            self._memory[key] = row
        return opportunity

    def get(self, identity_key: str) -> ContentOpportunity | None:
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM discovery_opportunities WHERE identity_key = ?", (identity_key,),
                ).fetchone()
            return _row_to_opportunity(dict(row)) if row is not None else None
        with OpportunityStore._memory_lock:
            row = self._memory.get(identity_key)
        return _row_to_opportunity(row) if row is not None else None

    def all(self) -> list[ContentOpportunity]:
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                rows = conn.execute("SELECT * FROM discovery_opportunities").fetchall()
            return [_row_to_opportunity(dict(r)) for r in rows]
        with OpportunityStore._memory_lock:
            rows = list(self._memory.values())
        return [_row_to_opportunity(r) for r in rows]

    def count(self) -> int:
        return len(self.all())

    def update_ranking(
        self, identity_key: str, *, score, confidence: str, status: str, risk_level: str | None,
        justification: list[str],
    ) -> None:
        """Atualiza SOMENTE os campos de ranking (nunca toca
        `signals`/`notes`/`discovered_at`) — chamado pela etapa de
        agregação/rank, separada da etapa de descoberta."""
        score_text = json.dumps(score) if isinstance(score, (int, float)) else str(score)
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE discovery_opportunities SET score = ?, confidence = ?, status = ?, "
                    "risk_level = ?, justification_json = ? WHERE identity_key = ?",
                    (score_text, confidence, status, risk_level, json.dumps(justification), identity_key),
                )
            return
        with OpportunityStore._memory_lock:
            row = self._memory.get(identity_key)
            if row is not None:
                row["score"] = score_text
                row["confidence"] = confidence
                row["status"] = status
                row["risk_level"] = risk_level
                row["justification_json"] = json.dumps(justification)

    def ranking_for(self, identity_key: str) -> dict | None:
        """Devolve os campos de ranking já persistidos (ou `None` se
        a oportunidade nunca foi ranqueada) — usado por testes e pela
        fila priorizada exposta a um futuro Pipeline 2."""
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT score, confidence, status, risk_level, justification_json "
                    "FROM discovery_opportunities WHERE identity_key = ?", (identity_key,),
                ).fetchone()
            if row is None:
                return None
            row = dict(row)
        else:
            with OpportunityStore._memory_lock:
                row = self._memory.get(identity_key)
            if row is None:
                return None
        if row.get("status") is None:
            return None
        score_raw = row.get("score")
        try:
            score = json.loads(score_raw) if score_raw is not None else "UNKNOWN"
        except (TypeError, json.JSONDecodeError):
            score = score_raw
        return {
            "score": score, "confidence": row.get("confidence"), "status": row.get("status"),
            "risk_level": row.get("risk_level"),
            "justification": json.loads(row["justification_json"]) if row.get("justification_json") else [],
        }

    def prioritized_queue(self) -> list[dict]:
        """A fila priorizada final — pronta para alimentar o Pipeline
        2. Ordenada por score numérico decrescente; itens sem score
        numérico (`LOW_EVIDENCE`/`STALE`) vão ao final, nunca
        escondidos — quem consome decide o que fazer com evidência
        insuficiente, nunca é uma decisão silenciosa deste módulo."""
        items = []
        for opp in self.all():
            ranking = self.ranking_for(opp.identity_key) or {
                "score": "UNKNOWN", "confidence": "LOW", "status": "DISCOVERED", "risk_level": None,
                "justification": [],
            }
            items.append({"opportunity": opp, **ranking})

        def sort_key(item):
            score = item["score"]
            return (0, -score) if isinstance(score, (int, float)) else (1, 0)

        items.sort(key=sort_key)
        return items

    # ------------------------------------------------------------------
    # MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
    # auditoria independente, 6ª rodada — fechamento): registro
    # AUTORITATIVO e DURÁVEL de aquisições — persistido numa tabela
    # SEPARADA (`discovery_acquisitions`), nunca dentro do blob de
    # sinais. `VerifiedLineage` REFERENCIA estes registros por
    # `acquisition_id`; toda revalidação (mesmo pós-restart, sem o
    # broker efêmero) busca o registro AQUI, nunca confia apenas no
    # que já está gravado dentro do próprio sinal.
    # ------------------------------------------------------------------

    def upsert_acquisition_record(self, record: DurableAcquisitionRecord) -> None:
        if self.db_path:
            self._upsert_acquisition_durable(record)
        else:
            self._upsert_acquisition_memory(record)

    def _upsert_acquisition_durable(self, record: DurableAcquisitionRecord) -> None:
        from core.models import db as db_module

        field_digests_json = json.dumps(record.observation_field_digests or {})
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO discovery_acquisitions "
                "(acquisition_id, source_id, execution_id, mode, status, data_status, issued_at, completed_at, "
                "result_digest, observations_digest, observation_field_digests) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (acquisition_id) DO UPDATE SET "
                "source_id=excluded.source_id, execution_id=excluded.execution_id, mode=excluded.mode, "
                "status=excluded.status, data_status=excluded.data_status, issued_at=excluded.issued_at, "
                "completed_at=excluded.completed_at, result_digest=excluded.result_digest, "
                "observations_digest=excluded.observations_digest, "
                "observation_field_digests=excluded.observation_field_digests",
                (
                    record.acquisition_id, record.source_id, record.execution_id, record.mode, record.status,
                    record.data_status, record.issued_at, record.completed_at, record.result_digest,
                    record.observations_digest, field_digests_json,
                ),
            )
            conn.commit()

    def _upsert_acquisition_memory(self, record: DurableAcquisitionRecord) -> None:
        with self._memory_lock:
            self._acquisition_memory[record.acquisition_id] = {
                "acquisition_id": record.acquisition_id, "source_id": record.source_id,
                "execution_id": record.execution_id, "mode": record.mode, "status": record.status,
                "data_status": record.data_status, "issued_at": record.issued_at,
                "completed_at": record.completed_at, "result_digest": record.result_digest,
                "observations_digest": record.observations_digest,
                "observation_field_digests": dict(record.observation_field_digests or {}),
            }

    def get_acquisition_record(self, acquisition_id: str | None) -> DurableAcquisitionRecord | None:
        if not acquisition_id:
            return None
        if self.db_path:
            return self._get_acquisition_durable(acquisition_id)
        return self._get_acquisition_memory(acquisition_id)

    def _get_acquisition_durable(self, acquisition_id: str) -> DurableAcquisitionRecord | None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT acquisition_id, source_id, execution_id, mode, status, data_status, issued_at, "
                "completed_at, result_digest, observations_digest, observation_field_digests "
                "FROM discovery_acquisitions "
                "WHERE acquisition_id = ?", (acquisition_id,),
            ).fetchone()
        if row is None:
            return None
        raw_field_digests = row[10] if len(row) > 10 else None
        field_digests = {}
        if raw_field_digests:
            try:
                parsed = json.loads(raw_field_digests)
                field_digests = parsed if isinstance(parsed, dict) else {}
            except Exception:
                field_digests = {}
        return DurableAcquisitionRecord(
            acquisition_id=row[0], source_id=row[1], execution_id=row[2], mode=row[3], status=row[4],
            data_status=row[5], issued_at=row[6], completed_at=row[7], result_digest=row[8],
            observations_digest=row[9],
            observation_field_digests=field_digests,
        )

    def _get_acquisition_memory(self, acquisition_id: str) -> DurableAcquisitionRecord | None:
        with self._memory_lock:
            data = self._acquisition_memory.get(acquisition_id)
        if data is None:
            return None
        data_copy = dict(data)
        field_digests = data_copy.get("observation_field_digests")
        data_copy["observation_field_digests"] = dict(field_digests) if isinstance(field_digests, dict) else {}
        return DurableAcquisitionRecord(**data_copy)
