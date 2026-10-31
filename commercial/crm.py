"""
commercial.crm
================
CRM V1 (Seção 47). Lifecycle: NEW -> ENGAGED -> QUALIFIED -> OFFERED ->
CUSTOMER | LOST | UNSUBSCRIBED.

MICROCORREÇÃO 5 (auditoria — reprovado na 4ª rodada):

- item 2 (TRATAMENTO COMPLETO DE FALHAS NO COMMIT): `BEGIN`, as
  gravações e o `COMMIT` agora vivem DENTRO do mesmo bloco try/except —
  antes, `conn.commit()` ficava num `else:` fora do try, então uma
  falha NO PRÓPRIO commit escapava sem rollback nem fechamento da
  conexão, deixando uma transação aberta e travando outros escritores.
  Agora qualquer falha (inserção, ou o commit em si) sempre passa por
  rollback + close antes de propagar, e um erro durante o rollback de
  limpeza nunca mascara o erro original (`raise` simples dentro do
  except preserva a exceção original mesmo após um try/except aninhado
  que trata a falha de rollback).

- item 3 (ATUALIZAÇÕES ATÔMICAS ENTRE INSTÂNCIAS): `transition()` não
  faz mais leitura e escrita como dois passos separados vulneráveis a
  corrida entre instâncias. O UPDATE agora é CONDICIONAL
  (`WHERE lead_id=? AND version=?`), verificando `cursor.rowcount`. Se
  0 linhas foram afetadas, outra instância já mudou o registro entre
  nossa leitura e nossa escrita — relemos o estado FRESCO e tentamos de
  novo (até um limite), sempre revalidando a transição contra o estado
  atual. Isso impede que uma atualização baseada em leitura obsoleta
  sobrescreva ou reverta uma transição (incluindo um estado terminal)
  já confirmada por outra instância.

Serviço interno; o caminho recomendado para chamadores externos/API é
sempre `core.governance.facade.GovernedFacade`.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
import time

from core.governance.idempotency import IdempotencyConflict, stable_fingerprint
from core.models import db as db_module
from core.models.entities import Lead, LeadStatus

_VALID_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    LeadStatus.NEW: {LeadStatus.ENGAGED, LeadStatus.LOST, LeadStatus.UNSUBSCRIBED},
    LeadStatus.ENGAGED: {LeadStatus.QUALIFIED, LeadStatus.LOST, LeadStatus.UNSUBSCRIBED},
    LeadStatus.QUALIFIED: {LeadStatus.OFFERED, LeadStatus.LOST, LeadStatus.UNSUBSCRIBED},
    LeadStatus.OFFERED: {LeadStatus.CUSTOMER, LeadStatus.LOST, LeadStatus.UNSUBSCRIBED},
    LeadStatus.CUSTOMER: {LeadStatus.UNSUBSCRIBED},
    LeadStatus.LOST: set(),
    LeadStatus.UNSUBSCRIBED: set(),
}

_DOMAIN = "lead"
_OPERATION = "create_lead"
_MAX_UPDATE_RETRIES = 5


class InvalidLeadTransition(Exception):
    pass


class LeadNotFound(KeyError):
    pass


class ConcurrentModificationError(Exception):
    """Levantado quando uma atualização condicional não consegue
    convergir após várias tentativas — indica contenção muito alta
    entre instâncias concorrentes no mesmo registro."""


class CRM:
    def __init__(self, *, db_path: str | None = None) -> None:
        self._leads: dict[str, Lead] = {}
        self._idempotency_index: dict[tuple, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._db_path = db_path
        if self._db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Persistência / leitura
    # ------------------------------------------------------------------

    def _row_to_lead(self, row: sqlite3.Row) -> Lead:
        return Lead(
            lead_id=row["lead_id"], channel_id=row["channel_id"], source=row["source"] or "UNKNOWN",
            campaign=row["campaign"], content_id=row["content_id"], offer_id=row["offer_id"],
            product_id=row["product_id"], status=LeadStatus(row["status"]),
            consent_state=row["consent_state"], conversion_state=row["conversion_state"],
            version=row["version"] if "version" in row.keys() else 0,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def _load_from_db(self) -> None:
        with db_module.connect(self._db_path) as conn:
            for row in conn.execute("SELECT * FROM leads").fetchall():
                lead = self._row_to_lead(row)
                self._leads[lead.lead_id] = lead
            for row in conn.execute(
                "SELECT * FROM idempotency_records WHERE domain = ? AND operation = ?", (_DOMAIN, _OPERATION)
            ).fetchall():
                key = (row["channel_id"], row["project_id"], row["idempotency_key"])
                self._idempotency_index[key] = (row["record_id"], row["fingerprint"])

    @staticmethod
    def _insert_lead_sql(conn: sqlite3.Connection, lead: Lead) -> None:
        conn.execute(
            "INSERT INTO leads "
            "(lead_id, channel_id, source, campaign, content_id, offer_id, product_id, status, "
            " consent_state, conversion_state, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lead.lead_id, lead.channel_id, lead.source, lead.campaign, lead.content_id, lead.offer_id,
             lead.product_id, lead.status.value, lead.consent_state, lead.conversion_state,
             lead.version, lead.created_at, lead.updated_at),
        )

    def _fetch_lead_from_db(self, lead_id: str) -> Lead | None:
        with db_module.connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return self._row_to_lead(row) if row is not None else None

    # ------------------------------------------------------------------
    # Operações
    # ------------------------------------------------------------------

    def create_lead(self, channel_id: str, *, idempotency_key: str | None = None, **kwargs) -> Lead:
        fingerprint = stable_fingerprint(
            channel_id, kwargs.get("source"), kwargs.get("campaign"), kwargs.get("content_id"),
            kwargs.get("offer_id"), kwargs.get("product_id"),
        )
        lead = Lead.create(channel_id, **kwargs)

        with self._lock:
            if not idempotency_key:
                # item 2: persiste ANTES de tocar o cache. Se a gravação
                # (inserção OU commit) falhar, o cache permanece intocado.
                if self._db_path:
                    self._insert_lead_atomic(lead)
                self._leads[lead.lead_id] = lead
                return lead

            index_key = (channel_id, "", idempotency_key)

            if self._db_path:
                return self._create_lead_durable(index_key, lead, fingerprint)

            existing = self._idempotency_index.get(index_key)
            if existing is not None:
                existing_id, existing_fp = existing
                if existing_fp != fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency_key '{idempotency_key}' já usada no canal '{channel_id}' "
                        "com conteúdo diferente — reutilização incompatível recusada"
                    )
                return self._leads[existing_id]

            self._leads[lead.lead_id] = lead
            self._idempotency_index[index_key] = (lead.lead_id, fingerprint)
            return lead

    def _insert_lead_atomic(self, lead: Lead) -> None:
        """Inserção simples (sem idempotency_key). `db_module.connect()`
        já cobre o item 2 corretamente: commit fica DENTRO do try
        interno do context manager, então uma falha no próprio commit
        também aciona rollback + close, sem deixar transação aberta."""
        with db_module.connect(self._db_path) as conn:
            self._insert_lead_sql(conn, lead)

    def _create_lead_durable(self, index_key: tuple, lead: Lead, fingerprint: str) -> Lead:
        channel_id, project_id, idempotency_key = index_key
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_lead_sql(conn, lead)
            conn.execute(
                "INSERT INTO idempotency_records "
                "(domain, operation, channel_id, project_id, idempotency_key, record_id, fingerprint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (_DOMAIN, _OPERATION, channel_id, project_id, idempotency_key, lead.lead_id, fingerprint, time.time()),
            )
            conn.commit()  # item 2: commit DENTRO do try — falha aqui também cai no except
        except sqlite3.IntegrityError:
            try:
                conn.rollback()
            except Exception:
                pass
            finally:
                conn.close()
            return self._resolve_lead_conflict(index_key, fingerprint)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass  # não mascara o erro original
            finally:
                conn.close()
            raise
        else:
            conn.close()
            self._leads[lead.lead_id] = lead
            self._idempotency_index[index_key] = (lead.lead_id, fingerprint)
            return lead

    def _resolve_lead_conflict(self, index_key: tuple, fingerprint: str) -> Lead:
        channel_id, project_id, idempotency_key = index_key
        row_conn = db_module.get_connection(self._db_path)
        try:
            row = row_conn.execute(
                "SELECT record_id, fingerprint FROM idempotency_records "
                "WHERE domain=? AND operation=? AND channel_id=? AND project_id=? AND idempotency_key=?",
                (_DOMAIN, _OPERATION, channel_id, project_id, idempotency_key),
            ).fetchone()
            if row is None:
                raise IdempotencyConflict(
                    f"idempotency_key '{idempotency_key}' colidiu mas o registro correspondente "
                    "não foi encontrado — estado inesperado, abortando com segurança"
                )
            existing_record_id, existing_fp = row["record_id"], row["fingerprint"]
            if existing_fp != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency_key '{idempotency_key}' já usada no canal '{channel_id}' "
                    "com conteúdo diferente — reutilização incompatível recusada"
                )
            existing_row = row_conn.execute(
                "SELECT * FROM leads WHERE lead_id = ?", (existing_record_id,)
            ).fetchone()
        finally:
            row_conn.close()
        existing_lead = self._row_to_lead(existing_row)
        self._leads[existing_lead.lead_id] = existing_lead
        self._idempotency_index[index_key] = (existing_record_id, existing_fp)
        return existing_lead

    def _conditional_update_lead(self, lead: Lead, *, expected_version: int) -> bool:
        """UPDATE condicionado à versão lida — item 3. `cursor.rowcount
        == 0` significa que outra instância já mudou o registro entre
        nossa leitura e esta escrita."""
        conn = db_module.get_autocommit_connection(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE leads SET status=?, updated_at=?, version=? WHERE lead_id=? AND version=?",
                (lead.status.value, lead.updated_at, lead.version, lead.lead_id, expected_version),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def transition(self, lead_id: str, new_status: LeadStatus) -> Lead:
        with self._lock:
            if not self._db_path:
                # modo em memória: self._lock já serializa totalmente —
                # sem outra instância possível, sem necessidade de
                # controle de versão condicional.
                if lead_id not in self._leads:
                    raise LeadNotFound(lead_id)
                current = self._leads[lead_id]
                allowed = _VALID_TRANSITIONS[current.status]
                if new_status not in allowed:
                    raise InvalidLeadTransition(
                        f"transição inválida: {current.status.value} -> {new_status.value}"
                    )
                updated = dataclasses.replace(
                    current, status=new_status, version=current.version + 1, updated_at=time.time()
                )
                self._leads[updated.lead_id] = updated
                return updated

            for _ in range(_MAX_UPDATE_RETRIES):
                current = self._fetch_lead_from_db(lead_id)
                if current is None:
                    raise LeadNotFound(lead_id)

                allowed = _VALID_TRANSITIONS[current.status]
                if new_status not in allowed:
                    raise InvalidLeadTransition(
                        f"transição inválida: {current.status.value} -> {new_status.value}"
                    )

                updated = dataclasses.replace(
                    current, status=new_status, version=current.version + 1, updated_at=time.time()
                )
                success = self._conditional_update_lead(updated, expected_version=current.version)
                if not success:
                    # outra instância mudou o lead entre a leitura e a
                    # escrita — relê o estado FRESCO e tenta de novo,
                    # revalidando a transição contra ele.
                    continue

                self._leads[updated.lead_id] = updated
                return updated

            raise ConcurrentModificationError(
                f"não foi possível atualizar o lead {lead_id} após {_MAX_UPDATE_RETRIES} tentativas "
                "— alta contenção entre instâncias concorrentes"
            )

    def get(self, lead_id: str) -> Lead:
        if self._db_path:
            lead = self._fetch_lead_from_db(lead_id)
            if lead is None:
                raise LeadNotFound(lead_id)
            self._leads[lead.lead_id] = lead
            return lead
        return self._leads[lead_id]

    def by_channel(self, channel_id: str) -> list[Lead]:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM leads WHERE channel_id = ?", (channel_id,)).fetchall()
            return [self._row_to_lead(r) for r in rows]
        return [lead for lead in self._leads.values() if lead.channel_id == channel_id]

    def by_channels(self, channel_ids: set[str]) -> list[Lead]:
        if not channel_ids:
            return []
        if self._db_path:
            placeholders = ",".join("?" * len(channel_ids))
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute(
                    f"SELECT * FROM leads WHERE channel_id IN ({placeholders})", tuple(channel_ids)
                ).fetchall()
            return [self._row_to_lead(r) for r in rows]
        return [lead for lead in self._leads.values() if lead.channel_id in channel_ids]

    def all_leads(self) -> list[Lead]:
        if self._db_path:
            with db_module.connect(self._db_path) as conn:
                rows = conn.execute("SELECT * FROM leads").fetchall()
            return [self._row_to_lead(r) for r in rows]
        return list(self._leads.values())

    def funnel_counts(self, channel_id: str) -> dict[str, int]:
        counts = {status.value: 0 for status in LeadStatus}
        for lead in self.by_channel(channel_id):
            counts[lead.status.value] += 1
        return counts
