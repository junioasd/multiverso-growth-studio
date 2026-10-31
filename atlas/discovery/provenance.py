"""
atlas.discovery.provenance
==============================
ATLAS PIPELINE 1 — fronteira de AQUISIÇÃO REAL, controlada pela
infraestrutura.

MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
auditoria independente, 6ª rodada — fechamento): `VerifiedLineage`
(ver `atlas.discovery.models`) não era ancorada a nenhum registro
INDEPENDENTE e AUTORITATIVO — `lineage.acquisition_id` podia ser
trocado por qualquer string, e enquanto `value` e seu digest
continuassem batendo entre si, nada detectava a adulteração; o digest
provava só "o valor não mudou desde que esta lineage foi escrita",
nunca "esta lineage ainda corresponde a uma aquisição durável
existente e íntegra".

Corrigido: `DurableAcquisitionRecord` — persistido numa tabela SQLite
SEPARADA (`discovery_acquisitions`, nunca dentro do blob do sinal),
o registro AUTORITATIVO de cada aquisição genuína. `VerifiedLineage`
REFERENCIA este registro (por `acquisition_id`), nunca o substitui —
toda revalidação (`DiscoverySignal.trust_issues()`) cruza a lineage
persistida no sinal contra ESTE registro independente, detectando
`source_id`/`execution_id`/`mode`/status/digest trocados, mesmo após
um restart sem o broker efêmero.

`TrustPolicy` — o contexto de validação (modo operacional vs. teste)
é agora avaliado A CADA validação, nunca fixado permanentemente na
criação da lineage — corrige a falha em que um sinal `mode="TEST"`,
uma vez aceito, continuava sustentando `READY_FOR_PIPELINE2` mesmo
após um restart em modo operacional (`allow_test_sources=False`).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = [
    "AcquisitionOutcome", "AcquisitionTransport", "DurableAcquisitionRecord", "NotImplementedTransport",
    "SourceAcquisitionBroker", "SourceExecutionContext", "SourceUnavailableForAcquisition", "TrustPolicy",
]

_MISSING = object()

# Janela padrão de validade de um `AcquisitionOutcome` COMPLETO — um
# resultado "envelhece" mesmo dentro da MESMA execução (nunca eterno);
# generosa o bastante para cobrir qualquer processamento razoável
# logo após a aquisição retornar.
_ACQUISITION_MAX_AGE_SECONDS = 300.0


class SourceUnavailableForAcquisition(Exception):
    """Levantada por `AcquisitionTransport.fetch()` quando a
    aquisição genuína falha (ou nunca foi implementada — ver
    `NotImplementedTransport`). Propaga através de `context.acquire()`
    até o adapter — que tipicamente a relança como
    `atlas.discovery.sources.SourceUnavailable`, tratada pelo motor
    sem derrubar o scan inteiro."""


@dataclass
class AcquisitionOutcome:
    """Resultado VIVO de uma aquisição GENUÍNA — construído
    EXCLUSIVAMENTE por `SourceAcquisitionBroker._acquire()`, NUNCA
    pelo adapter. Expõe `.observations` — as OBSERVAÇÕES reais
    obtidas (campo → valor, tal como veio do transporte, nunca
    inventado) — para que um adapter tenha dado GENUÍNO a transformar
    em sinais. `data_status` classifica honestamente o conteúdo:
    `"DATA"` quando há ao menos uma observação; `"NO_DATA"` quando o
    transporte não encontrou nada (ou devolveu `None`/algo inesperado
    — NUNCA promovido a `"DATA"` por engano). Efêmero — em memória,
    escopado ao broker — NUNCA persistido diretamente; usado só no
    momento da sanitização para verificar sinais e produzir um
    `atlas.discovery.models.VerifiedLineage` durável."""
    acquisition_id: str
    source_id: str
    execution_id: str
    issued_at: float
    completed_at: float
    status: str  # sempre "COMPLETED" -- resultados falhos nunca são construídos aqui, a exceção é levantada em vez disso
    mode: str  # "REAL" | "TEST"
    data_status: str  # "DATA" | "NO_DATA"
    observations: dict = field(default_factory=dict)
    result_digest: str = ""

    def is_valid_for(
        self, *, source_id: str, execution_id: str, now: float | None = None,
        max_age_seconds: float = _ACQUISITION_MAX_AGE_SECONDS,
    ) -> bool:
        """`True` somente quando o resultado está genuinamente
        `COMPLETED`, pertence à MESMA fonte e à MESMA execução
        informadas, e ainda está dentro da janela de validade — nunca
        aprova por omissão, nunca aceita um resultado de outra fonte
        ou de uma execução anterior. NUNCA verifica `data_status`
        aqui — "DATA" vs "NO_DATA" é uma decisão de CONTEÚDO, tratada
        separadamente por quem consome (nunca confundida com validade
        estrutural do resultado)."""
        if self.status != "COMPLETED":
            return False
        if self.source_id != source_id or self.execution_id != execution_id:
            return False
        current = now if now is not None else time.time()
        return (current - self.completed_at) <= max_age_seconds

    def observation(self, field_name: str):
        """Devolve a observação genuína para `field_name`, ou
        `_MISSING` (nunca `None` — `None` pode ser um valor
        observado legítimo) se o campo não existir nesta aquisição."""
        return self.observations.get(field_name, _MISSING)

    def has_observation(self, field_name: str) -> bool:
        return field_name in self.observations

    def _observations_digest(self) -> str:
        try:
            digest_source = repr(sorted(self.observations.items(), key=lambda kv: repr(kv[0])))
        except Exception:
            digest_source = repr(self.observations)
        return hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _observation_field_digests(self) -> dict[str, str]:
        digests: dict[str, str] = {}
        for k, v in self.observations.items():
            digests[str(k)] = hashlib.sha256(repr(v).encode("utf-8", errors="replace")).hexdigest()[:16]
        return digests

    def to_durable_record(self) -> "DurableAcquisitionRecord":
        """MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada
        por auditoria independente, 6ª rodada — fechamento): o
        registro AUTORITATIVO que `ContentDiscoveryEngine.scan_source()`
        persiste numa tabela SEPARADA (`discovery_acquisitions`) —
        NUNCA dentro do blob do sinal — para que uma `VerifiedLineage`
        possa ser revalidada contra uma prova independente, mesmo após
        um restart sem este `AcquisitionOutcome` (efêmero) existir
        mais."""
        return DurableAcquisitionRecord(
            acquisition_id=self.acquisition_id, source_id=self.source_id, execution_id=self.execution_id,
            mode=self.mode, status=self.status, data_status=self.data_status, issued_at=self.issued_at,
            completed_at=self.completed_at, result_digest=self.result_digest,
            observations_digest=self._observations_digest(),
            observation_field_digests=self._observation_field_digests(),
        )


@dataclass(frozen=True)
class DurableAcquisitionRecord:
    """Registro AUTORITATIVO e DURÁVEL de uma aquisição genuína —
    persistido numa tabela SQLite SEPARADA do blob do sinal
    (`discovery_acquisitions`, ver `core/models/db.py`), sobrevive a
    qualquer restart sem depender do `SourceAcquisitionBroker`
    efêmero. `VerifiedLineage` (`atlas.discovery.models`) REFERENCIA
    este registro por `acquisition_id` — nunca o substitui, nunca o
    duplica. Nenhum segredo/token/credencial é persistido aqui — só
    metadados de proveniência e um digest das observações (nunca o
    payload bruto)."""
    acquisition_id: str
    source_id: str
    execution_id: str
    mode: str
    status: str
    data_status: str
    issued_at: float
    completed_at: float
    result_digest: str
    observations_digest: str
    observation_field_digests: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.observation_field_digests is None:
            object.__setattr__(self, "observation_field_digests", {})
        elif not isinstance(self.observation_field_digests, dict):
            object.__setattr__(self, "observation_field_digests", dict(self.observation_field_digests))


@dataclass(frozen=True)
class TrustPolicy:
    """MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
    auditoria independente, 6ª rodada — fechamento): o contexto de
    validação — nunca fixado permanentemente na criação de uma
    `VerifiedLineage`, sempre avaliado A CADA chamada de
    `DiscoverySignal.trust_issues()`. Isso corrige a falha em que um
    sinal `mode="TEST"`, uma vez aceito, continuava sustentando
    `READY_FOR_PIPELINE2` mesmo após um restart em modo operacional —
    o `mode` gravado na lineage NUNCA muda (a origem é sempre honesta
    e identificável), mas se ele é ACEITO depende da política ATUAL,
    reavaliada a cada vez. O padrão (`allow_test_sources=False`) é o
    mais seguro — nunca aceita `TEST` por omissão."""
    allow_test_sources: bool = False
    now: float | None = None


class AcquisitionTransport(ABC):
    """O ÚNICO lugar onde uma chamada externa genuína aconteceria.
    `mode` distingue REAL (produção) de TEST (determinístico, uso
    exclusivo de teste) — determinado pela CLASSE de transporte que a
    infraestrutura registrou no broker, nunca por um atributo
    autodeclarado no adapter."""
    mode: str = "REAL"

    @abstractmethod
    def fetch(self, *, request: dict) -> dict | None:
        """Executa a aquisição de verdade (ou a simula
        deterministicamente, em modo TEST). Devolve um `dict` de
        OBSERVAÇÕES genuínas (`{campo: valor}`) — ou `None`/`{}`
        quando a fonte não encontrou nada (NUNCA um erro — isso vira
        `data_status="NO_DATA"`, não uma exceção). Levanta
        `SourceUnavailableForAcquisition` em caso de falha REAL —
        nunca devolve silenciosamente algo inválido para disfarçar uma
        falha."""


class NotImplementedTransport(AcquisitionTransport):
    """Transporte PADRÃO para QUALQUER fonte sem integração real
    implementada nesta entrega — SEMPRE levanta. Nenhuma integração é
    inventada só para fazer um teste passar; o modo operacional
    honesto continua UNAVAILABLE/NOT_IMPLEMENTED até uma integração
    real genuína ser conectada por uma infraestrutura futura."""
    mode = "REAL"

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id

    def fetch(self, *, request: dict) -> dict | None:
        raise SourceUnavailableForAcquisition(
            f"{self.source_id}: nenhuma integração real de API implementada nesta entrega (NOT_IMPLEMENTED) — "
            f"presença na lista de fontes nunca concede confiança operacional por si só"
        )


@dataclass
class SourceExecutionContext:
    """Passado a `DiscoverySource.discover()` no lugar de um
    identificador livre — a ÚNICA via pela qual o adapter pode
    SOLICITAR uma aquisição verificável, com as OBSERVAÇÕES reais
    obtidas. Nunca permite ao adapter construir um `AcquisitionOutcome`
    sozinho; só o broker pode."""
    source_id: str
    execution_id: str
    _broker: "SourceAcquisitionBroker" = field(repr=False)

    def acquire(self, *, request: dict | None = None) -> AcquisitionOutcome:
        return self._broker._acquire(source_id=self.source_id, execution_id=self.execution_id, request=request or {})


def _classify_data_status(raw) -> tuple:
    """MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
    independente, 5ª rodada): `None`, um tipo inesperado (não-`dict`),
    ou um `dict` VAZIO NUNCA viram `"DATA"` — sempre `"NO_DATA"`,
    honestamente, com `observations={}`. Só um `dict` genuinamente
    não-vazio é `"DATA"`."""
    if isinstance(raw, dict) and raw:
        return "DATA", dict(raw)
    return "NO_DATA", {}


class SourceAcquisitionBroker:
    """Controla a ÚNICA via de aquisição verificável. Transportes são
    registrados EXPLICITAMENTE pela infraestrutura (nunca
    autodeclarados pelo adapter, nunca derivados da simples presença
    de uma fonte em `sources=[...]`) — sem um transporte registrado
    para um `source_id`, qualquer tentativa de aquisição falha
    honestamente (mesmo comportamento de `NotImplementedTransport`)."""

    def __init__(self) -> None:
        self._transports: dict[str, AcquisitionTransport] = {}
        self._results: dict[str, AcquisitionOutcome] = {}

    def register_transport(self, source_id: str, transport: AcquisitionTransport) -> None:
        self._transports[source_id] = transport

    def has_transport(self, source_id: str) -> bool:
        return source_id in self._transports

    def transport_mode(self, source_id: str) -> str | None:
        transport = self._transports.get(source_id)
        return transport.mode if transport is not None else None

    def context_for(self, *, source_id: str, execution_id: str) -> SourceExecutionContext:
        return SourceExecutionContext(source_id=source_id, execution_id=execution_id, _broker=self)

    def _acquire(self, *, source_id: str, execution_id: str, request: dict) -> AcquisitionOutcome:
        transport = self._transports.get(source_id)
        if transport is None:
            raise SourceUnavailableForAcquisition(f"{source_id}: nenhum transporte de aquisição registrado")
        raw = transport.fetch(request=request)  # pode levantar -- propagado sem tratamento aqui, fail-closed
        data_status, observations = _classify_data_status(raw)
        try:
            digest_source = repr(sorted(observations.items(), key=lambda kv: repr(kv[0])))
        except Exception:
            digest_source = repr(observations)
        digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:16]
        outcome = AcquisitionOutcome(
            acquisition_id=uuid.uuid4().hex, source_id=source_id, execution_id=execution_id,
            issued_at=time.time(), completed_at=time.time(), status="COMPLETED", mode=transport.mode,
            data_status=data_status, observations=observations, result_digest=digest,
        )
        self._results[outcome.acquisition_id] = outcome
        return outcome

    def get_result(self, acquisition_id: str | None) -> AcquisitionOutcome | None:
        if not acquisition_id:
            return None
        return self._results.get(acquisition_id)

    def results_for(self, *, source_id: str, execution_id: str) -> list[AcquisitionOutcome]:
        """Todas as aquisições genuinamente produzidas para esta
        fonte+execução — usado pela sanitização para localizar QUAL
        `AcquisitionOutcome` sustenta cada sinal (um `discover()` pode
        chamar `context.acquire()` mais de uma vez)."""
        return [o for o in self._results.values() if o.source_id == source_id and o.execution_id == execution_id]
