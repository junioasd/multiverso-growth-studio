"""
core.multimodal.commercial_signals
====================================
COMANDO 4/6 — sinais COMERCIAIS a partir de evidência multimodal real.

Os analisadores reais (`real_*_analyzer`) extraem PROPRIEDADES MEDIDAS
(status HTTP, bytes, título, texto, páginas, dimensões, duração, hash) —
corretamente FACT. Faltava a camada comercial: promessa, oferta, preço,
CTA, prova social, garantia, ângulo de venda e inconsistência
mensagem × oferta — com rastreabilidade e sem transformar interpretação
em fato.

Regra rígida:
- PRESENÇA LITERAL de um trecho no material é FACT (cita o trecho exato,
  a posição e o hash da evidência de origem);
- INTERPRETAÇÃO (o que é "a promessa", "o preço da oferta", o "ângulo")
  é INFERENCE, com confiança limitada;
- AMBIGUIDADE (ex.: vários preços distintos) é UNKNOWN com o motivo —
  nunca fato, nunca escolha arbitrária;
- semântica VISUAL (o que uma imagem/thumbnail "mostra") sem modelo de
  visão configurado é UNKNOWN — nada é inventado a partir de pixels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality

_PRICE_RE = re.compile(r"(R\$|US\$|€|\$)\s?(\d{1,3}(?:[.\s]\d{3})*(?:,\d{2})?|\d+(?:[.,]\d{2})?)")
_CTA_RE = re.compile(r"\b(compre agora|comprar agora|garanta (?:já|a sua|o seu|sua vaga)|quero (?:comprar|garantir|"
                     r"minha vaga)|inscreva-se|assine agora|baixe agora|adquira|matricule-se|comece agora)\b", re.I)
_PROOF_RE = re.compile(r"\b(\d[\d.]*\s?(?:mil\s)?(?:alunos|clientes|compradores|avaliações|depoimentos)|"
                       r"depoimentos?|avaliações|★+)", re.I)
_GUARANTEE_RE = re.compile(r"garantia(?: incondicional)? de \d+\s?dias", re.I)
_URGENCY_RE = re.compile(r"\b(últimas vagas|só hoje|somente hoje|oferta por tempo limitado|termina (?:hoje|em)|"
                         r"vagas limitadas)\b", re.I)
_FREE_RE = re.compile(r"\b(grátis|gratuito|gratuita|de graça|sem custo)\b", re.I)

_VISUAL_MODALITIES = {Modality.IMAGE, Modality.VIDEO, Modality.SCREENSHOT}


@dataclass(frozen=True)
class CommercialSignal:
    name: str
    value: object
    claim_type: ClaimType
    confidence: float
    evidence_id: str
    reference_hash: str | None
    source: str
    quote: str = ""            # trecho literal que sustenta o sinal
    span: tuple = ()           # (início, fim) no texto extraído
    note: str = ""


def _money(raw: str) -> float | None:
    digits = raw.replace(" ", "")
    if "," in digits:
        digits = digits.replace(".", "").replace(",", ".")
    try:
        return round(float(digits), 2)
    except ValueError:
        return None


def extract_commercial_signals(evidence) -> list:
    """Sinais comerciais rastreáveis a partir de UMA evidência multimodal."""
    base = dict(evidence_id=evidence.evidence_id, reference_hash=evidence.reference_hash, source=evidence.source)
    if evidence.confidence <= 0.0:
        return [CommercialSignal(name="analysis", value=None, claim_type=ClaimType.UNKNOWN, confidence=0.0,
                                 note=f"evidência indisponível: {evidence.summary}", **base)]
    if evidence.modality in _VISUAL_MODALITIES and not evidence.content_text:
        return [CommercialSignal(
            name="visual_semantics", value=None, claim_type=ClaimType.UNKNOWN, confidence=0.0,
            note="nenhum modelo de visão configurado: o que a imagem/vídeo MOSTRA (rosto, texto na arte, "
                 "estrutura visual) é DESCONHECIDO — só as propriedades medidas da evidência são fato", **base)]
    text = evidence.content_text or ""
    if not text.strip():
        return [CommercialSignal(name="text", value=None, claim_type=ClaimType.UNKNOWN, confidence=0.0,
                                 note="evidência sem texto extraído", **base)]
    signals = []

    def literal(name, regex):
        found = []
        for m in regex.finditer(text):
            found.append(m)
            signals.append(CommercialSignal(name=name, value=m.group(0), claim_type=ClaimType.FACT, confidence=1.0,
                                            quote=m.group(0), span=(m.start(), m.end()),
                                            note="presença literal no material", **base))
        return found

    prices = literal("price_mention", _PRICE_RE)
    ctas = literal("cta_phrase", _CTA_RE)
    proofs = literal("social_proof_mention", _PROOF_RE)
    literal("guarantee_mention", _GUARANTEE_RE)
    urgency = literal("urgency_mention", _URGENCY_RE)
    free = list(_FREE_RE.finditer(text))

    headline = text.split("\n", 1)[0].strip()
    if headline:
        signals.append(CommercialSignal(
            name="promise", value=headline[:200], claim_type=ClaimType.INFERENCE, confidence=0.5,
            quote=headline[:200], span=(0, min(len(headline), 200)),
            note="o título/cabeçalho foi INTERPRETADO como promessa principal", **base))

    distinct = sorted({v for v in (_money(m.group(2)) for m in prices) if v is not None})
    if len(distinct) == 1:
        m = prices[0]
        signals.append(CommercialSignal(
            name="offer_price", value=distinct[0], claim_type=ClaimType.INFERENCE, confidence=0.6,
            quote=m.group(0), span=(m.start(), m.end()),
            note="único valor monetário no material INTERPRETADO como preço da oferta", **base))
    elif len(distinct) > 1:
        signals.append(CommercialSignal(
            name="offer_price", value=None, claim_type=ClaimType.UNKNOWN, confidence=0.0,
            quote="; ".join(m.group(0) for m in prices[:5]),
            note=f"AMBÍGUO: {len(distinct)} valores distintos ({distinct}) — preço da oferta não determinado "
                 f"(pode ser 'de/por', parcelamento, bônus)", **base))
    if proofs:
        signals.append(CommercialSignal(name="uses_social_proof", value=True, claim_type=ClaimType.INFERENCE,
                                        confidence=0.6, quote=proofs[0].group(0),
                                        span=(proofs[0].start(), proofs[0].end()),
                                        note="menção literal INTERPRETADA como prova social", **base))
    if urgency:
        signals.append(CommercialSignal(name="sales_angle", value="urgência/escassez", claim_type=ClaimType.INFERENCE,
                                        confidence=0.5, quote=urgency[0].group(0),
                                        span=(urgency[0].start(), urgency[0].end()),
                                        note="ângulo de venda INFERIDO de expressões de urgência", **base))
    if free and distinct:
        f = free[0]
        signals.append(CommercialSignal(
            name="message_offer_inconsistency", value=f"'{f.group(0)}' × preço {distinct}",
            claim_type=ClaimType.INFERENCE, confidence=0.5, quote=f.group(0), span=(f.start(), f.end()),
            note="mensagem sugere gratuidade e o material também cita preço — POSSÍVEL inconsistência "
                 "(pode ser bônus gratuito); requer revisão humana", **base))
    if not ctas:
        signals.append(CommercialSignal(name="cta_phrase", value=None, claim_type=ClaimType.UNKNOWN, confidence=0.0,
                                        note="nenhuma chamada para ação reconhecida no texto — DESCONHECIDO "
                                             "(pode existir só como imagem/botão)", **base))
    return signals


def record_signals_in_twin(twin, *, entity_type: str, entity_id: str, signals: list, ttl_seconds: float,
                           recorded_by: str, now: float | None = None) -> list:
    """Projeta sinais no Gêmeo PRESERVANDO a classe: FACT literal →
    FACT (fonte = evidência multimodal, ref = evidência#trecho);
    INFERENCE → INFERENCE com base declarada; UNKNOWN → NADA é gravado
    (desconhecido é ausência). Quando existe EXATAMENTE um valor
    monetário no material, registra o FATO literal `single_listed_amount`
    (base para a Visão Evolutiva de preço)."""
    import time as _t

    now = _t.time() if now is None else now
    recorded = []
    by_name: dict = {}
    for sig in signals:
        by_name.setdefault(sig.name, []).append(sig)
    for name, group in by_name.items():
        sig = group[0]
        if sig.claim_type == ClaimType.UNKNOWN or sig.value is None:
            continue
        source_id = f"multimodal:{sig.source}"
        ref = f"{sig.evidence_id}@{','.join(str(g.span) for g in group)}#{sig.reference_hash or ''}"
        value = sorted({str(g.value) for g in group}) if sig.claim_type == ClaimType.FACT and len(group) > 1 else sig.value
        recorded.append(twin.record(
            entity_type=entity_type, entity_id=entity_id, attribute=f"signal.{name}", value=value,
            claim_type=sig.claim_type, source_id=source_id, source_ref=ref,
            basis="" if sig.claim_type == ClaimType.FACT else sig.note, observed_at=now,
            valid_until=now + ttl_seconds, confidence=max(sig.confidence, 0.01), quality=0.8,
            confirmed=sig.claim_type == ClaimType.FACT, recorded_by=recorded_by, now=now))
    amounts = sorted({v for v in (_money(m.group(2)) for s_ in by_name.get("price_mention", [])
                                  for m in [_PRICE_RE.search(s_.quote)] if m) if v is not None})
    if len(amounts) == 1:
        first = by_name["price_mention"][0]
        recorded.append(twin.record(
            entity_type=entity_type, entity_id=entity_id, attribute="single_listed_amount", value=amounts[0],
            claim_type=ClaimType.FACT, source_id=f"multimodal:{first.source}",
            source_ref=f"{first.evidence_id}@{first.span}#{first.reference_hash or ''}", observed_at=now,
            valid_until=now + ttl_seconds, confidence=1.0, quality=0.8, confirmed=True, recorded_by=recorded_by,
            now=now))
    return recorded
