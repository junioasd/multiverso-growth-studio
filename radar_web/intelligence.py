"""
radar_web.intelligence
========================
COMANDO 4/6 — INTELIGÊNCIA COMERCIAL ACIONÁVEL + PRIORIZAÇÃO RASTREÁVEL.

Transforma evidência do Gêmeo Digital em BRIEF de oportunidade com todos
os campos exigidos — e o que não tem base fica em `dados_faltantes`, nunca
preenchido. Um brief nunca nasce VALIDADO: validação só acontece pelo
ciclo adaptativo (resultado real × previsão).

Priorização sem "ranking mágico": cada critério é um número em [0, 1]
declarado COM a sua origem; o score é a soma ponderada explícita e o
retorno mostra a contribuição de cada critério. Oportunidade com evidência
em CONFLITO ou sem fato vigente não é ranqueada (fica em
`not_ranked`, com o motivo). Estratégias refutadas/obsoletas no histórico
do alvo REDUZEM a prioridade de forma explícita.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from atlas.digital_twin import ReadingStatus

BRIEF_FIELDS = ("oportunidade", "evidencia", "mercado", "publico", "problema", "produto_oferta", "canal",
                "concorrencia", "custo_estimado", "risco", "hipotese", "metrica_sucesso", "prazo_teste_dias",
                "criterio_abandono", "nivel_confianca", "dados_faltantes")

DEFAULT_WEIGHTS = {"evidence_strength": 0.25, "economic_potential": 0.15, "margin": 0.10, "cost": 0.10,
                   "risk": 0.10, "effort": 0.05, "time_to_learn": 0.05, "reversibility": 0.05,
                   "resource_fit": 0.05, "history": 0.05, "data_confidence": 0.05}
LOWER_IS_BETTER = {"cost", "risk", "effort", "time_to_learn"}


@dataclass
class OpportunityBrief:
    opportunity_id: str
    status: str                           # CANDIDATE | BLOCKED_CONFLICT | INSUFFICIENT_EVIDENCE
    fields: dict
    evidence_readings: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def build_brief(twin, *, opportunity_id: str, entity_type: str, entity_id: str, context: dict,
                now: float | None = None) -> OpportunityBrief:
    """`context` traz o que o ATLAS/operador DECLAROU (mercado, público,
    canal, hipótese...) — cada valor é tratado como declaração, nunca como
    fato observado. Evidência vem só do Gêmeo."""
    readings = twin.snapshot(entity_type=entity_type, entity_id=entity_id, now=now)
    current = [r for r in readings if r.status == ReadingStatus.CURRENT]
    conflicts = [r for r in readings if r.status == ReadingStatus.CONFLICT]
    fields = {k: context.get(k) for k in BRIEF_FIELDS if k not in ("evidencia", "nivel_confianca", "dados_faltantes")}
    fields["oportunidade"] = fields.get("oportunidade") or opportunity_id
    fields["evidencia"] = [{"atributo": r.attribute, "status": r.status.value, "valor_factual": r.fact_value,
                            "fontes": [f.source_id for f in r.facts],
                            "nao_factual": [(f.claim_type.value, f.value) for f in r.non_factual]}
                           for r in readings]
    confidences = [f.confidence for r in current for f in r.facts]
    fields["nivel_confianca"] = round(min(confidences), 3) if confidences else 0.0
    fields["dados_faltantes"] = [k for k in BRIEF_FIELDS
                                 if k not in ("evidencia", "nivel_confianca", "dados_faltantes", "oportunidade")
                                 and fields.get(k) in (None, "", [])]
    if conflicts:
        status = "BLOCKED_CONFLICT"
        reasons = [f"{r.attribute}: " + "; ".join(r.notes) for r in conflicts]
    elif not current:
        status = "INSUFFICIENT_EVIDENCE"
        reasons = ["nenhum fato vigente sustenta a oportunidade (DESCONHECIDO / EVIDÊNCIA INSUFICIENTE)"]
    else:
        status, reasons = "CANDIDATE", []
    return OpportunityBrief(opportunity_id=opportunity_id, status=status, fields=fields,
                            evidence_readings=readings, reasons=reasons)


def prioritize(items: list, *, weights: dict | None = None) -> dict:
    """`items`: [{"brief": OpportunityBrief, "criteria": {nome: (valor, origem)},
    "history_penalty": (valor, origem) opcional}]. Devolve ranking com a
    decomposição de cada score e a lista do que não pôde ser ranqueado."""
    weights = dict(weights or DEFAULT_WEIGHTS)
    ranked, not_ranked = [], []
    for item in items:
        brief = item["brief"]
        if brief.status != "CANDIDATE":
            not_ranked.append({"opportunity": brief.opportunity_id, "status": brief.status, "reasons": brief.reasons})
            continue
        contributions, missing = {}, []
        for name, w in weights.items():
            entry = item.get("criteria", {}).get(name)
            if entry is None:
                missing.append(name)
                continue
            value, origin = entry
            if not (0.0 <= float(value) <= 1.0) or not origin:
                raise ValueError(f"critério '{name}' precisa de valor em [0,1] E origem declarada")
            v = 1.0 - float(value) if name in LOWER_IS_BETTER else float(value)
            contributions[name] = {"valor": float(value), "origem": origin, "peso": w,
                                   "contribuicao": round(w * v, 6)}
        penalty = item.get("history_penalty")
        penalty_value = 0.0
        if penalty is not None:
            penalty_value, penalty_origin = penalty
            contributions["penalidade_historico"] = {"valor": -float(penalty_value), "origem": penalty_origin}
        score = round(sum(c.get("contribuicao", 0.0) for c in contributions.values()) - float(penalty_value), 6)
        ranked.append({"opportunity": brief.opportunity_id, "score": score, "por_que": contributions,
                       "criterios_ausentes": missing, "confianca_dados": brief.fields["nivel_confianca"]})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return {"ranked": ranked, "not_ranked": not_ranked, "weights": weights}


def history_penalty_for(adaptive_cycle, *, target_type: str, target_id: str) -> tuple | None:
    """Penalidade EXPLÍCITA derivada do ciclo adaptativo real: estratégias
    refutadas ou validadas num regime que mudou reduzem a prioridade."""
    from atlas.learning import StrategyStatus

    same = [s for s in adaptive_cycle.strategies() if (s.target_type, s.target_id) == (target_type, target_id)]
    refuted = [s.strategy_id for s in same if s.status == StrategyStatus.ABANDONED]
    stale = [s.strategy_id for s in same if s.status == StrategyStatus.NEEDS_REVALIDATION]
    if not refuted and not stale:
        return None
    return (min(0.5, 0.2 * len(refuted) + 0.1 * len(stale)),
            f"ciclo adaptativo: refutadas={refuted} regime_mudou={stale}")


# ----------------------------------------------------------------------
# COMANDO 4/6 — RADAR CONTÍNUO sobre o `JobQueue` existente (mesmo padrão
# do acionamento de planos do Comando 3). Cada observação é um job
# idempotente (`radar_obs:<entity>:<tick>`); após processar, o próximo
# tick é reagendado — o histórico do Gêmeo acumula observações no tempo
# e a Visão Evolutiva (`detect_changes`) passa a ter série real.
# ----------------------------------------------------------------------
RADAR_OBSERVATION_JOB = "radar_observe_page"


def schedule_observation(queue, *, url: str, entity_id: str, tick: int = 1, ttl_seconds: float = 3 * 24 * 3600.0):
    return queue.enqueue(job_type=RADAR_OBSERVATION_JOB, idempotency_key=f"radar_obs:{entity_id}:{tick}",
                         payload={"url": url, "entity_id": entity_id, "tick": tick, "ttl_seconds": ttl_seconds},
                         max_attempts=3)


def process_observation_jobs(queue, director, *, reschedule: bool = True) -> list:
    """Worker do RADAR: observa cada página pendente pelo caminho real do
    diretor (`radar_observe_page`, auditado) e reagenda o próximo tick.
    Fonte indisponível NÃO grava nada e NÃO interrompe o monitoramento."""
    from core.models.entities import JobState

    from core.operations import kill_switch as _ks   # v1.0.1

    if _ks.block_reason(getattr(director, "db_path", None)):
        return []
    done = []
    for job in list(queue.all()):
        if job.job_type != RADAR_OBSERVATION_JOB or job.state not in (JobState.QUEUED, JobState.RETRYING):
            continue
        if queue.try_start(job.job_id) is None:
            continue
        p = job.payload
        try:
            out = director.radar_observe_page(url=p["url"], entity_id=p["entity_id"], ttl_seconds=p["ttl_seconds"])
        except Exception as exc:
            queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
            continue
        queue.complete(job.job_id, {"facts_recorded": len(out["recorded"]),
                                    "available": out["evidence"].confidence > 0})
        if reschedule:
            schedule_observation(queue, url=p["url"], entity_id=p["entity_id"], tick=p["tick"] + 1,
                                 ttl_seconds=p["ttl_seconds"])
        done.append((p["entity_id"], len(out["recorded"])))
    return done
