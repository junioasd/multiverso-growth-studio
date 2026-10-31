"""Evidências reproduzíveis do Comando Mestre 2/6. Rodar na raiz do projeto:
    python evidencias_comando_2.py
Caminho REAL: build_app -> login do Owner -> GovernedFacade -> SQLite -> Gêmeo."""
import os, sys, tempfile
sys.path.insert(0, os.getcwd())
from api.bootstrap import build_app
from atlas.digital_twin import EvidenceRequirement
from atlas.learning import TestDesign
from core.models import db as db_module
from core.models.entities import ValueType
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password

fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd); db_module.remove_db_file(path)
app = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=path, init_database=True)
owner = principal_from_session(app.auth.current_session(app.auth.login("o@t.com", "TestPassword123")))
F, A, k = app.facade, app.atlas, [0]
def key(): k[0] += 1; return f"e{k[0]}"
def ev(ch="chan_1", camp=None, clicks=0, sales=0, amount=100.0, vt=ValueType.REAL):
    for _ in range(clicks): F.record_sales_event(owner, channel_id=ch, event_type="click", value_type=vt, campaign_id=camp, idempotency_key=key())
    for _ in range(sales): F.record_sales_event(owner, channel_id=ch, event_type="sale", value_type=vt, amount=amount, campaign_id=camp, idempotency_key=key())
def fin(t, amt, ch="chan_1"): F.record_finance_entry(owner, entry_type=t, amount=amt, channel_id=ch, idempotency_key=key())

print("=" * 78); print("1. SEPARAÇÃO FATO / INFERÊNCIA / HIPÓTESE / PREVISÃO"); print("=" * 78)
ev(clicks=40, sales=2, amount=150.0)                      # REAL
ev(sales=6, amount=150.0, vt=ValueType.FORECAST)          # previsão
ev(sales=1, amount=150.0, vt=ValueType.ESTIMATED)         # estimativa
fin("revenue", 300.0); fin("expense", 120.0)
A.sync_digital_twin()
c = A.consultation(); c.switch_context("channel", "chan_1")
for attr in ("revenue", "margin", "conversion_rate", "audience_size"):
    a = c.ask_state(attr)
    print(f"- {attr}: {a['statement']}")
    for f in a["facts"]: print(f"    FACT        {f['value']!r:>8} fonte={f['source']} ref={f['ref']}")
    for n in a["non_factual"]: print(f"    {n['class']:<11} {n['value']!r:>8} base='{n['basis']}'")

print(); print("=" * 78); print("2. CONFLITO ENTRE FONTES E DECISÃO BLOQUEADA"); print("=" * 78)
ev(ch="chan_2", clicks=10, sales=3, amount=150.0); fin("revenue", 300.0, ch="chan_2"); A.sync_digital_twin()
c.switch_context("channel", "chan_2"); print("- revenue chan_2:", c.ask_state("revenue")["statement"], "|", c.ask_state("revenue")["uncertainty"])
d = A.decide_with_evidence(topic="budget", scope="channel:chan_2", chosen="aumentar orçamento", justification="-",
                           requirements=[EvidenceRequirement("channel", "chan_2", "revenue"), EvidenceRequirement("channel", "chan_2", "margin")])
print(f"- decisão: {d.status.value} | motivos: {list(d.evidence)}")

print(); print("=" * 78); print("3. CICLO ADAPTATIVO (dados reais, controle = diferença-em-diferenças)"); print("=" * 78)
cy = A.adaptive_cycle
for camp in ("camp_t", "camp_c", "camp_bad"): ev(camp=camp, clicks=100, sales=2 if camp != "camp_bad" else 4)
A.sync_digital_twin()
good = cy.define_hypothesis(target_type="campaign", target_id="camp_t", metric_name="conversion_rate",
                            hypothesis="novo gancho eleva a conversão", expected_value=0.05, design=TestDesign.CONTROLLED, control_id="camp_c")
bad = cy.define_hypothesis(target_type="campaign", target_id="camp_bad", metric_name="conversion_rate",
                           hypothesis="thumbnail escura eleva a conversão", expected_value=0.06)
print(f"observar/interpretar: linha de base FATO camp_t=0.02, camp_c=0.02, camp_bad=0.04")
print(f"hipóteses registradas como HYPOTHESIS no Gêmeo; previsões como FORECAST; previsão também no loop com baseline")
for i in range(5):
    if i: cy.start_round(good.strategy_id); cy.start_round(bad.strategy_id)
    ev(camp="camp_t", clicks=100, sales=5); ev(camp="camp_c", clicks=100, sales=2); ev(camp="camp_bad", clicks=100, sales=1)
    A.sync_digital_twin()
    rg = cy.measure_round(good.strategy_id); rb = cy.measure_round(bad.strategy_id)
    print(f" rodada {i+1}: [controlado] previsto={rg.predicted_value:.4f} real(DiD)={rg.actual_value:.4f} erro={abs(rg.error):.4f} dir_ok={rg.direction_correct}"
          f" | [refutável] previsto={rb.predicted_value:.4f} real={rb.actual_value:.4f} dir_ok={rb.direction_correct}")
for s in (cy.get(good.strategy_id), cy.get(bad.strategy_id)):
    print(f"- {s.hypothesis}: status={s.status.value} causal={s.causal_status.value} avaliação={s.last_evaluation}")
for camp in ("camp_t", "camp_bad"):
    r = cy.recommend(target_type="campaign", target_id=camp, metric_name="conversion_rate")
    print(f"- adaptar {camp}: {r['action'].value} — {r['why']}")
c.switch_context("campaign", "camp_bad"); p = c.propose(bad.strategy_id, "SCALE")
print(f"- Owner propõe escalar camp_bad -> {p['status']}: {p['statement'][:90]}")

print(); print("=" * 78); print("4. MEMÓRIA APÓS RESTART"); print("=" * 78)
app2 = build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password("TestPassword123"), db_path=path, init_database=False)
c2 = app2.atlas.consultation(); c2.switch_context("channel", "chan_2"); r = c2.recall("budget")
print(f"- nova sessão recupera decisão {r['decision_id']}: {r['status']} | {r['statement']}")
print(f"- estratégias recuperadas: {[(s.status.value, s.causal_status.value) for s in app2.atlas.adaptive_cycle.strategies()]}")
db_module.remove_db_file(path)
