"""
atlas.discovery.models
=========================
ATLAS PIPELINE 1 — INTELIGÊNCIA E DESCOBERTA.

Modelo de dados de oportunidades de CONTEÚDO (tema, formato, gancho,
plataforma) — distinto de `atlas.radar.models.CommercialOpportunity`
(oportunidades comerciais/de oferta) — nunca duplica esse módulo,
apenas REUTILIZA seus tipos de evidência já validados.

Reaproveitado sem duplicação:
- `atlas.radar.models.EvidenceLevel` — REAL/ESTIMATED/INFERRED/UNKNOWN,
  classificação por SINAL individual.
- `core.evidence.claims.ClaimType` — FACT/ESTIMATE/INFERENCE/
  HYPOTHESIS/UNKNOWN, a distinção obrigatória exigida pelo pedido
  ("diferencie fato, inferência e hipótese").

Toda `DiscoverySignal` carrega OBRIGATORIAMENTE fonte (`source`), data
de observação (`observed_at`), confiança (`confidence`) e vigência
(`valid_until`) — nunca um valor solto. Um sinal sem fonte real por
trás é SEMPRE `EvidenceLevel.UNKNOWN` — nunca aproximado a REAL/
ESTIMATED/INFERRED "porque parece razoável".
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from atlas.discovery.provenance import DurableAcquisitionRecord, TrustPolicy
from atlas.radar.models import EvidenceLevel
from core.evidence.claims import ClaimType

__all__ = [
    "ContentFormat", "ContentOpportunity", "DiscoverySignal", "Platform", "RankedContentOpportunity",
    "VerifiedLineage", "compute_value_digest",
]

# MICROCORREÇÃO CRÍTICA (proveniência — falha real encontrada por
# auditoria independente): pares `EvidenceLevel`/`ClaimType`
# semanticamente INCOERENTES — nunca aceitos, mesmo com o resto da
# proveniência (fonte/confiança/data) preenchido corretamente.
# `FACT` (a afirmação mais forte) só é coerente com `REAL` (dado
# genuinamente observado) — uma estimativa ou inferência NUNCA é um
# "fato verificado". `REAL` nunca é coerente com `HYPOTHESIS` — um
# dado realmente observado não é especulativo por definição.
_INCOHERENT_EVIDENCE_CLAIM_PAIRS = frozenset({
    (EvidenceLevel.ESTIMATED, ClaimType.FACT),
    (EvidenceLevel.INFERRED, ClaimType.FACT),
    (EvidenceLevel.REAL, ClaimType.HYPOTHESIS),
})

# Faixa permitida para `confidence` — nunca fora de [0.0, 1.0], e
# EXIGIDA estritamente maior que zero quando `evidence != UNKNOWN`
# (uma afirmação com confiança zero não é, na prática, uma afirmação).
_CONFIDENCE_MIN_EXCLUSIVE = 0.0
_CONFIDENCE_MAX_INCLUSIVE = 1.0

# Tolerância para `observed_at` no futuro — relógios ligeiramente
# dessincronizados entre processos/máquinas não devem, sozinhos,
# invalidar uma observação genuína feita há poucos segundos.
_OBSERVED_AT_FUTURE_TOLERANCE_SECONDS = 5.0


def _is_coherent_evidence_claim_pair(evidence: EvidenceLevel, claim_type: ClaimType) -> bool:
    return (evidence, claim_type) not in _INCOHERENT_EVIDENCE_CLAIM_PAIRS


class Platform(str, Enum):
    """Canais de conteúdo cobertos explicitamente pelo pedido — mais
    um `OTHER` de escape-hatch, mesma disciplina de
    `atlas.radar.models.ChannelType.OTHER`."""
    YOUTUBE = "youtube"
    YOUTUBE_SHORTS = "youtube_shorts"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    INSTAGRAM_REELS = "instagram_reels"
    OTHER = "other"


class ContentFormat(str, Enum):
    SHORT_FORM_VIDEO = "short_form_video"
    LONG_FORM_VIDEO = "long_form_video"
    LIVE = "live"
    CAROUSEL = "carousel"
    STORY = "story"
    OTHER = "other"


# Sinais avaliados no mínimo por oportunidade de conteúdo — nomes
# usados como chaves em `ContentOpportunity.signals`.
#
# MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
# independente, 4ª rodada): `evidence_quality` foi REMOVIDO deste
# contrato — existia como sinal autodeclarável pela fonte, mas NUNCA
# influenciava o ranking (um campo presente no contrato, mas
# silenciosamente ignorado). A representação CANÔNICA de qualidade de
# evidência já existe e já é usada de verdade: a confiança MÉDIA
# (`confidence`) dos sinais que efetivamente contribuem para o score
# (multiplicando o resultado final — ver `ContentDiscoveryEngine._score()`).
# Um campo `evidence_quality` SEPARADO e autodeclarado pela própria
# fonte reintroduziria exatamente o tipo de autodeclaração não
# verificável que a fronteira de aquisição (`atlas.discovery.provenance`)
# existe para eliminar — nunca reintroduzido.
DISCOVERY_SIGNAL_KEYS = (
    "trend_strength", "audience", "estimated_reach", "competition", "hook_strength",
    "production_cost", "risk", "compliance",
)

# MICROCORREÇÃO CRÍTICA (freshness — falha real encontrada por
# auditoria independente, 3ª rodada): `valid_until=None` NUNCA
# significa "validade infinita" para um sinal ESSENCIAL usado em
# inteligência ATUAL — significa "validade desconhecida", sujeita a
# esta política de freshness PADRÃO e DOCUMENTADA (nunca eterna).
# Sinais de tendência de conteúdo mudam rápido — 24h é uma janela
# razoável e documentada, não um valor arbitrário escondido.
DEFAULT_FRESHNESS_WINDOW_SECONDS = 24 * 60 * 60.0


def _is_valid_timestamp(value: Any) -> bool:
    """MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
    independente, 4ª rodada): `valid_until`/`observed_at` malformados
    (string, `bool`, `None` fora de lugar, `NaN`, `Infinity`) NUNCA
    podem derrubar uma comparação (`TypeError: '<=' not supported...`)
    — toda comparação temporal passa por aqui primeiro, fail-closed
    (valor inválido nunca é tratado como válido)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def compute_value_digest(value: Any) -> str:
    """Hash determinístico do VALOR de um sinal — usado por
    `VerifiedLineage.verified_value_digest` para detectar, mesmo após
    um restart (sem acesso ao broker efêmero), se o valor foi
    adulterado DEPOIS de ter sido verificado contra a aquisição."""
    return hashlib.sha256(repr(value).encode("utf-8", errors="replace")).hexdigest()[:16]


# MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
# auditoria independente, 6ª rodada — fechamento): DOMAIN POLICY
# CANÔNICA e CENTRALIZADA — lineage genuína (proveniência íntegra,
# aquisição verificada) NUNCA transforma um valor semanticamente
# IMPOSSÍVEL para sua métrica em dado válido. Antes, `trend_strength
# =50.0`/`audience=-100`/`competition=-4.0` passavam com lineage
# válida e `trust_issues()` vazio. Documentado aqui, verificado em
# UM único lugar (`_domain_issue_for()`, chamado de dentro de
# `trust_issues()`) — nunca copiado em múltiplos pontos.
_SIGNAL_DOMAINS: dict = {
    "trend_strength": (0.0, 1.0),
    "competition": (0.0, 1.0),
    "risk": (0.0, 1.0),
    "hook_strength": (0.0, 1.0),
    "compliance": (0.0, 1.0),  # semântica: grau de conformidade regulatória/política, 0=nenhuma, 1=total — definido explicitamente aqui, nunca assumido
    "audience": (0.0, None),
    "estimated_reach": (0.0, None),
    "production_cost": (0.0, None),
}


def _domain_issue_for(key: str | None, value: Any) -> str | None:
    """`None` quando o valor está dentro do domínio da métrica `key`
    (ou quando `key` não tem política de domínio definida — nunca
    bloqueia por omissão aqui, já que outras checagens de tipo/
    finitude em `provenance_issues()` já cobrem isso). Uma string
    descrevendo o problema caso contrário — nunca um clamp silencioso:
    um valor fora do domínio é REJEITADO, nunca corrigido."""
    if key is None:
        return None
    domain = _SIGNAL_DOMAINS.get(key)
    if domain is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"valor de '{key}' não é numérico ({value!r})"
    if not math.isfinite(value):
        return f"valor de '{key}' não é finito ({value!r})"
    lo, hi = domain
    if value < lo:
        return f"valor de '{key}' abaixo do domínio mínimo {lo} ({value!r})"
    if hi is not None and value > hi:
        return f"valor de '{key}' acima do domínio máximo {hi} ({value!r})"
    return None


@dataclass(frozen=True)
class VerifiedLineage:
    """MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada por
    auditoria independente, 5ª rodada — fechamento): prova DURÁVEL e
    AUTOCONTIDA de que, no momento da sanitização
    (`ContentDiscoveryEngine._verify_against_outcome()`), este sinal
    foi genuinamente verificado contra observações reais de uma
    aquisição — persistida JUNTO com o sinal (mesmo blob JSON da
    oportunidade), NUNCA depende do `SourceAcquisitionBroker` efêmero
    para ser revalidada depois (que não sobrevive a um restart, por
    design). `verified_value_digest` é um hash do VALOR do sinal NO
    MOMENTO em que a verificação passou — se o valor for adulterado
    depois (diretamente no armazenamento, por exemplo), o digest deixa
    de bater e `DiscoverySignal.trust_issues()` detecta isso mesmo sem
    nenhum acesso ao broker.

    `acquisition_observations_digest` (MICROCORREÇÃO ARQUITETURAL
    CRÍTICA — falha real encontrada por auditoria independente, 6ª
    rodada — fechamento): um retrato do
    `DurableAcquisitionRecord.observations_digest` — o registro
    AUTORITATIVO e SEPARADO da aquisição (`atlas.discovery.provenance`)
    — capturado NO MOMENTO da verificação. `trust_issues()` sempre
    busca o `DurableAcquisitionRecord` atual (por `acquisition_id`) e
    cruza CADA campo contra o que está gravado aqui — `acquisition_id`
    trocado, `source_id`/`execution_id`/`mode` trocados, ou o registro
    simplesmente ausente, TODOS resultam em falha, mesmo que
    `verified_value_digest` (o valor do SINAL) continue batendo."""
    source_id: str
    execution_id: str
    acquisition_id: str
    mode: str  # "REAL" | "TEST" -- do transporte que produziu a aquisição, nunca autodeclarado
    verified_at: float
    observation_fields: tuple = ()  # quais campos da aquisição sustentam este sinal
    verified_value_digest: str = ""
    acquisition_observations_digest: str = ""


@dataclass
class DiscoverySignal:
    """Um único atributo avaliado — NUNCA um valor solto. Carrega
    `evidence` (nível de confiabilidade do VALOR em si — mesma
    disciplina de `atlas.radar.models.OpportunitySignal`) e
    `claim_type` (que TIPO de afirmação isso representa — FATO
    observável, estimativa, inferência derivada, ou hipótese ainda
    não testada) — as duas classificações são complementares, nunca
    redundantes: um valor pode ser `EvidenceLevel.REAL` (veio de uma
    API real) mas `ClaimType.INFERENCE` (é uma dedução A PARTIR do
    dado real, não o dado em si).

    `claimed_observation_fields`/`lineage` (MICROCORREÇÃO
    ARQUITETURAL CRÍTICA — falha real encontrada por auditoria
    independente, 5ª rodada, fechamento — substitui o antigo
    `acquisition_id` solto): o ADAPTER declara, em
    `claimed_observation_fields`, quais campos da aquisição sustentam
    o valor deste sinal — mas isso é só uma DECLARAÇÃO de intenção,
    nunca prova por si só. `lineage` só é preenchido pelo MOTOR
    (`ContentDiscoveryEngine._verify_against_outcome()`), DEPOIS de
    confirmar — contra as observações REAIS da aquisição ao vivo —
    que, para `FACT`, o valor declarado é IDÊNTICO ao observado; para
    `ESTIMATE`/`INFERENCE`, que os campos referenciados genuinamente
    existem na aquisição (sem exigir igualdade direta, já que é uma
    estimativa/inferência, não o dado bruto). Sem essa verificação —
    ou se ela falhar — o sinal é sanitizado para `UNKNOWN` antes de
    ser persistido; `lineage` nunca é construído por atalho. Ver
    `atlas.discovery.provenance` e `trust_issues()`."""
    value: Any
    evidence: EvidenceLevel = EvidenceLevel.UNKNOWN
    claim_type: ClaimType = ClaimType.UNKNOWN
    source: str | None = None
    observed_at: float = field(default_factory=time.time)
    confidence: float = 0.0  # 0.0-1.0 — nunca inventado; 0.0 quando evidence=UNKNOWN
    valid_until: float | None = None  # vigência EXPLÍCITA — None significa "desconhecida", nunca "permanente" (ver is_fresh())
    claimed_acquisition_id: str | None = None  # declarado pelo ADAPTER -- QUAL aquisição sustenta este sinal (intenção, nunca prova)
    claimed_observation_fields: tuple = ()  # declarado pelo ADAPTER -- QUAIS campos dessa aquisição (intenção, nunca prova)
    lineage: VerifiedLineage | None = None  # só preenchido pelo MOTOR, após verificação contra a aquisição ao vivo

    def is_expired(self, *, now: float | None = None) -> bool:
        """Vigência EXPLICITAMENTE vencida — só significativo quando
        `valid_until` está definido. Para a decisão completa de
        "ainda conta como atual" (incluindo o caso `valid_until=None`),
        use `is_fresh()`, nunca este método isoladamente.

        MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        independente, 4ª rodada): `valid_until` malformado (string,
        `bool`, `NaN`, `Infinity`) NUNCA levanta — tratado como
        expirado/inválido, fail-closed, nunca como "ainda válido"."""
        if self.valid_until is None:
            return False
        if not _is_valid_timestamp(self.valid_until):
            return True
        return (now if now is not None else time.time()) > self.valid_until

    def is_fresh(
        self, *, now: float | None = None, max_age_seconds: float = DEFAULT_FRESHNESS_WINDOW_SECONDS,
    ) -> bool:
        """MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        independente, 3ª rodada): antes, `valid_until=None` era
        tratado, na prática, como vigência infinita (`is_expired()`
        nunca dizia `True` para ele) — um sinal observado há 1 ano,
        sem vigência explícita, ainda sustentava `READY_FOR_PIPELINE2`.
        Corrigido: quando `valid_until` está definido (e é um
        timestamp GENUÍNO — ver abaixo), vigente enquanto `now <=
        valid_until`. Quando `valid_until` é `None` (desconhecido),
        aplica a política de freshness PADRÃO sobre `observed_at` —
        vigente somente enquanto `now - observed_at <= max_age_seconds`
        — NUNCA eterno.

        MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        independente, 4ª rodada): `valid_until`/`observed_at`
        malformados (string, `bool`, `None` fora de lugar, `NaN`,
        `Infinity`) NUNCA levantam `TypeError` — sempre tratados como
        `False` (nunca fresh), fail-closed. Uma vigência explicitamente
        anterior à observação (`valid_until < observed_at`, quando
        ambos são timestamps genuínos) é logicamente incoerente —
        também nunca fresh."""
        current = now if now is not None else time.time()
        if self.valid_until is not None:
            if not _is_valid_timestamp(self.valid_until):
                return False
            if _is_valid_timestamp(self.observed_at) and self.valid_until < self.observed_at:
                return False  # incoerente: vigência terminando antes de ter sido observado
            return current <= self.valid_until
        if not _is_valid_timestamp(self.observed_at):
            return False
        return (current - self.observed_at) <= max_age_seconds

    def provenance_issues(self, *, now: float | None = None) -> list[str]:
        """MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        independente): mecanismo EXPLÍCITO e REUTILIZÁVEL de
        proveniência — `EvidenceLevel.REAL` (ou qualquer valor
        diferente de `UNKNOWN`) SOZINHO nunca é prova suficiente. Uma
        fonte/adaptador NUNCA consegue transformar mera autodeclaração
        em fato confiável apenas setando `evidence=REAL` — precisa
        TAMBÉM fornecer proveniência mínima coerente e completa.
        Devolve a lista de problemas encontrados (vazia = proveniência
        válida) — usado tanto por `has_valid_provenance()` quanto para
        compor razões diagnósticas explícitas no Quality Gate. Futuros
        adapters REAIS devem popular `source`/`claim_type`/
        `confidence`/`observed_at` corretamente — este método é o
        contrato reutilizável que eles precisam satisfazer."""
        issues: list[str] = []
        if self.evidence == EvidenceLevel.UNKNOWN:
            issues.append("evidence=UNKNOWN")
            return issues  # sem evidência alguma, as demais checagens não se aplicam
        if not self.source or not str(self.source).strip():
            issues.append("source ausente/vazio")
        if self.claim_type == ClaimType.UNKNOWN:
            issues.append("claim_type=UNKNOWN")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            issues.append(f"confidence não numérica ({self.confidence!r})")
        elif not math.isfinite(self.confidence):
            issues.append(f"confidence não finita ({self.confidence!r})")
        elif not (_CONFIDENCE_MIN_EXCLUSIVE < self.confidence <= _CONFIDENCE_MAX_INCLUSIVE):
            issues.append(f"confidence fora do intervalo permitido (0, 1] ({self.confidence!r})")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)):
            issues.append(f"observed_at inválido ({self.observed_at!r})")
        elif not math.isfinite(self.observed_at) or self.observed_at <= 0:
            issues.append(f"observed_at inválido ({self.observed_at!r})")
        else:
            current = now if now is not None else time.time()
            if self.observed_at > current + _OBSERVED_AT_FUTURE_TOLERANCE_SECONDS:
                issues.append("observed_at está no futuro")
        if self.claim_type != ClaimType.UNKNOWN and not _is_coherent_evidence_claim_pair(self.evidence, self.claim_type):
            issues.append(f"combinação incoerente evidence={self.evidence.value}/claim_type={self.claim_type.value}")
        return issues

    def has_valid_provenance(self, *, now: float | None = None) -> bool:
        """`True` somente quando `provenance_issues()` não encontra
        NENHUM problema — nunca aprova por omissão."""
        return not self.provenance_issues(now=now)

    def trust_issues(
        self, *, key: str | None = None, now: float | None = None, policy: TrustPolicy | None = None,
        durable_record: DurableAcquisitionRecord | None = None,
    ) -> list[str]:
        """MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real encontrada
        por auditoria independente, 5ª e 6ª rodadas — fechamento): a
        validação CANÔNICA ÚNICA —

            proveniência sintática (`provenance_issues()`)
                +
            domínio da métrica (`_domain_issue_for()`, quando `key` é
            informado — nunca copiado em múltiplos pontos)
                +
            integridade do `lineage` (valor não adulterado desde a
            verificação)
                +
            âncora durável (`durable_record` — o
            `DurableAcquisitionRecord` AUTORITATIVO e SEPARADO,
            cruzado campo a campo contra o que a lineage do sinal
            gravou)
                +
            política de confiança ATUAL (`policy` — decide, a CADA
            chamada, nunca na criação, se `mode="TEST"` é aceito)

        — reutilizada EXATAMENTE nos mesmos pontos, nunca
        reimplementada: sanitização (depois de tentar construir o
        `lineage`), Quality Gate, ranking (para QUALQUER sinal usado,
        essencial ou opcional) — e persistência/restart, que reusam
        os MESMOS dois caminhos (gate/ranking) depois de buscar o
        `durable_record` atual no armazenamento. `policy=None` usa o
        padrão mais seguro (`TrustPolicy()`, `allow_test_sources=False`)
        — nunca permissivo por omissão. Sem `lineage`, sem
        `durable_record`, com qualquer campo do registro durável
        divergente da lineage, com o valor fora do domínio da métrica,
        ou com `mode="TEST"` não permitido pela política atual — NUNCA
        aprova, `UNKNOWN`/`LOW_EVIDENCE`, nunca `READY`."""
        issues = self.provenance_issues(now=now)
        if issues:
            return issues

        domain_issue = _domain_issue_for(key, self.value)
        if domain_issue:
            issues.append(domain_issue)

        if self.lineage is None:
            issues.append("sem lineage verificada (nenhuma prova durável de aquisição vinculada)")
            return issues
        if not self.lineage.observation_fields:
            issues.append("lineage sem campos de observação referenciados")
        current_digest = compute_value_digest(self.value)
        if current_digest != self.lineage.verified_value_digest:
            issues.append("valor não corresponde mais ao digest verificado na lineage (possível adulteração)")

        # MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        # independente, 6ª rodada — fechamento): a lineage sozinha
        # NUNCA é suficiente — precisa ser cruzada contra o registro
        # AUTORITATIVO e SEPARADO (`durable_record`). Sem ele, ou com
        # qualquer campo divergente, a lineage é tratada como órfã.
        if durable_record is None:
            issues.append("sem registro durável de aquisição para revalidar (nunca aprova por omissão)")
        else:
            if durable_record.acquisition_id != self.lineage.acquisition_id:
                issues.append("acquisition_id do registro durável não corresponde à lineage")
            if durable_record.source_id != self.lineage.source_id:
                issues.append("source_id do registro durável não corresponde à lineage")
            if durable_record.execution_id != self.lineage.execution_id:
                issues.append("execution_id do registro durável não corresponde à lineage")
            if durable_record.mode != self.lineage.mode:
                issues.append("mode do registro durável não corresponde à lineage")
            if durable_record.status != "COMPLETED":
                issues.append("registro durável não está COMPLETED")
            if durable_record.data_status != "DATA":
                issues.append("registro durável não tem data_status=DATA")
            if durable_record.observations_digest != self.lineage.acquisition_observations_digest:
                issues.append("observations_digest do registro durável não corresponde à lineage (possível adulteração)")

            field_digests = getattr(durable_record, "observation_field_digests", None)
            if not field_digests or not isinstance(field_digests, dict):
                issues.append("registro durável sem âncora campo-valor (observation_field_digests ausente ou vazio)")
            else:
                for f in self.lineage.observation_fields:
                    if f not in field_digests:
                        issues.append(f"campo '{f}' referenciado pela lineage não existe em observation_field_digests do registro durável")
                    elif self.claim_type == ClaimType.FACT and field_digests[f] != current_digest:
                        issues.append(f"digest do campo '{f}' no registro durável não corresponde ao valor do sinal (possível adulteração)")

        # MICROCORREÇÃO CRÍTICA (falha real encontrada por auditoria
        # independente, 6ª rodada — fechamento): a política é sempre a
        # ATUAL, nunca a que valia quando a lineage foi criada — um
        # restart em modo operacional (`allow_test_sources=False`)
        # nunca deve aceitar uma lineage `mode="TEST"`, mesmo que ela
        # tenha sido genuinamente aceita antes, sob outra política.
        current_policy = policy if policy is not None else TrustPolicy()
        if self.lineage.mode == "TEST" and not current_policy.allow_test_sources:
            issues.append("lineage_mode=TEST não permitido pela política de confiança atual")

        return issues

    def has_valid_trust(
        self, *, key: str | None = None, now: float | None = None, policy: TrustPolicy | None = None,
        durable_record: DurableAcquisitionRecord | None = None,
    ) -> bool:
        """`True` somente quando `trust_issues()` não encontra NENHUM
        problema — nunca aprova por omissão."""
        return not self.trust_issues(key=key, now=now, policy=policy, durable_record=durable_record)


@dataclass
class ContentOpportunity:
    opportunity_id: str
    platform: Platform
    theme: str
    content_format: ContentFormat
    hook: str
    signals: dict[str, DiscoverySignal] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    discovered_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def signal(self, key: str) -> DiscoverySignal:
        return self.signals.get(key, DiscoverySignal(value="UNKNOWN", evidence=EvidenceLevel.UNKNOWN))

    @property
    def identity_key(self) -> str:
        """Chave de deduplicação DETERMINÍSTICA — nunca
        `opportunity_id` (aleatório, só para rastreio interno da
        execução). A MESMA oportunidade (mesmo tema/formato/gancho/
        plataforma) descoberta de novo produz a MESMA `identity_key`
        — normalizada (minúsculas, espaços colapsados) para que
        pequenas variações de formatação não gerem duplicatas
        espúrias."""
        def norm(s):
            return " ".join(str(s).strip().lower().split())
        return f"{self.platform.value}:{norm(self.theme)}:{self.content_format.value}:{norm(self.hook)}"

    @staticmethod
    def new_id() -> str:
        return f"cop_{uuid.uuid4().hex[:12]}"


@dataclass
class RankedContentOpportunity:
    opportunity: ContentOpportunity
    score: float | str  # "UNKNOWN" quando a evidência é insuficiente para pontuar com confiança
    confidence: str  # LOW | MEDIUM | HIGH — nunca "CERTAIN"
    status: str  # READY_FOR_PIPELINE2 | LOW_EVIDENCE | STALE
    justification: list[str] = field(default_factory=list)
    risk_level: str | None = None
