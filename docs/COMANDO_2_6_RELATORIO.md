# COMANDO MESTRE 2/6 — ATLAS + GÊMEO DIGITAL ADAPTATIVO — RELATÓRIO FINAL

**Baseline oficial:** `multiverso_growth_studio_fechamento_1_6_final_2026-09-24.zip`
(SHA-256 `2dbd654dfa754d5fecc2cef7bf89e8f6cd4960a03525ede385670ab9267e91a9`, verificado e preservado).
**Ambiente:** Linux, Python 3.12, sem rede.
**Invocação:** `python -W error::ResourceWarning -m unittest discover -s tests -t . -v`

## 1. Resultado

| Testes | PASS | FAIL | ERROR | SKIP | RC | ResourceWarning |
|---|---|---|---|---|---|---|
| 1.927 | 1.925 | 0 | 0 | 2 | 0 | 0 |

Os 1.927 testes somam os 1.895 do baseline e os 32 novos (`tests/test_comando_2_atlas_gemeo_digital.py`). Nenhum teste foi removido nem enfraquecido.

Os **2 skips** são `test_real_windows_*`. Eles exigem Windows real e já foram executados e aprovados no Windows durante o Comando 1 (seção 8 de `docs/FECHAMENTO_1_6_RELATORIO.md`).

## 2. Estado real do ATLAS antes deste comando

| Aspecto | Estado encontrado |
|---|---|
| Dados reais | Eventos de venda (6 dimensões + `ValueType`), livro financeiro, catálogo (canais, produtos, ofertas, funis), leads, observabilidade de integrações, sinais de descoberta, missões |
| Validação | Forte na borda: `ValueType` obrigatório, `UnverifiedAmountRejected`, gates de evidência da descoberta e da biblioteca |
| Versionamento | Eventos só acrescentados; nenhum estado versionado de entidade |
| Expiração | Só nos sinais de descoberta (`valid_until`) |
| Conflito entre fontes | Inexistente |
| Memória de decisões | Nenhuma efetiva (`WorkingMemory.record_decision`: volátil e sem consumidor em produção) |
| Aprendizado | Loop do ATLAS volátil e "apenas informativo"; o loop quantitativo não fechava o ciclo |

## 3. Causas-raiz das lacunas
1. **Defeito de produção:** `core.learning.loop.LearningRecord.direction_correct` comparava apenas o sinal do previsto e do realizado. Para métricas de nível não-negativas, isso é tautológico: previsto 10% contra real ~1% (erro de ~900%) em 5 rodadas foi promovido a `VALIDATED` com `direction_accuracy = 1.0`.
2. **Taxonomia incompleta:** `ClaimType` não tinha a classe PREVISÃO/SIMULAÇÃO.
3. **Ausências estruturais:** modelo de estado com proveniência, validade, versão e conflito; gate de evidência para decisões; memória de decisões persistente; fechamento do ciclo (recalibrar e adaptar).
4. **Falha de regressão encontrada no fechamento, já corrigida:** a guarda `test_every_third_party_import_listed_in_requirements` acusava `py_compile`.
   - Gatilho: o teste de compilação do Comando 2.
   - Causa-raiz: **defeito de teste**. A lista `_KNOWN_STDLIB` estava incompleta; `py_compile` é stdlib (`sys.stdlib_module_names`).
   - Não era defeito de produção nem de ambiente (a lista é estática).
   - Correção: incluir `py_compile` na lista.
   - Força da guarda preservada: mutantes com `yaml`, `requests` e `pandas` injetados em produção continuam reprovados (3 de 3).

## 4. Arquitetura da integração ATLAS + Gêmeo Digital

```
GovernedFacade ──► SQLite (vendas, finanças, catálogo, leads, observabilidade, descoberta, missões)
                     │  sync_from_operational_store()  (somente SELECT)
                     ▼
            atlas.digital_twin.DigitalTwin ── atlas_twin_facts (append-only, versionado)
             │  reading() → status: CURRENT | UNKNOWN | NOT_OBSERVED | EXPIRED | CONFLICT | LOW_CONFIDENCE | UNCONFIRMED
             ├─► EvidenceGate ─────────► AtlasDirector.decide_with_evidence (DECIDED | BLOCKED)
             ├─► AdaptiveStrategyCycle (atlas.learning)
             │      HYPOTHESIS/FORECAST no Gêmeo + Prediction(baseline) no core.learning.loop
             │      medir (FATO novo) → comparar → aprender → recalibrar → adaptar (SCALE|TEST_WITH_CONTROL|CONTINUE_TEST|ABANDON|ABSTAIN)
             ├─► DecisionLedger ── atlas_decisions (persistente; inclui BLOCKED/ABSTAINED/DISAGREED)
             └─► AtlasConsultation (diálogo estruturado: contexto, estado, recall, why, propose)
```

**Modelado a partir de dados reais disponíveis:**
- canais, campanhas, funis, produtos e plataformas;
- origem de tráfego;
- receita (vendas e livro financeiro), custos realizados e comprometidos, margem;
- eventos e conversão;
- leads (audiência) e público declarado;
- concorrência e mercado (sinais de descoberta com classe e validade próprias);
- estado das integrações;
- desempenho operacional (missões);
- política de autoridade do ATLAS (RBAC).

**Sem fonte, fica `UNKNOWN` (nada inventado):** tamanho de audiência medido, CAC e LTV sem insumos, preço de concorrente.

**Metadados de cada afirmação:** origem (`source_id` + `source_ref`, que referencia as linhas reais), `observed_at`, `valid_until`, qualidade, confiança, confirmação, versão, `supersedes` e `derived_from`.

**Por que um único módulo novo:** nenhuma estrutura existente modelava estado factual versionado. Todo o resto foi incorporado a estruturas existentes (`core.learning.loop`, `core.evidence.claims`, `atlas.learning`, `atlas.director`, `core.models.db`).

## 5. Arquivos modificados
- **Produção:**
  - `atlas/digital_twin.py` (novo)
  - `atlas/learning.py`
  - `atlas/director.py`
  - `core/learning/loop.py`
  - `core/evidence/claims.py`
  - `core/models/db.py` (3 tabelas aditivas e 2 colunas aditivas)
- **Testes:**
  - `tests/test_comando_2_atlas_gemeo_digital.py` (novo, 32 testes)
  - `tests/test_microcorrecao_seguranca_bloco_final2.py` (1 nome stdlib na lista)
- **Comando 1 intacto:** `graph_executor.py`, os testes `test_fechamento_1_6_*` e os helpers são **idênticos, byte a byte,** ao baseline; seus testes seguem verdes.

## 6. Testes por categoria (arquivo dedicado)
| Categoria | Classe de teste |
|---|---|
| Compilação | `CompilationTests` |
| Proveniência | `ProvenanceTests` |
| Atualização e expiração | `UpdateAndExpiryTests` |
| Conflito entre fontes | `SourceConflictTests` |
| Histórico | `HistoryTests` |
| Separação de classes | `EpistemicSeparationTests` |
| Decisão bloqueada | `EvidenceBlockedDecisionTests` |
| Ciclo adaptativo | `AdaptiveCycleTests` |
| Previsão × realidade | `PredictionVsRealityTests` |
| Conversacionais | `AtlasConversationTests` |

**Sensibilidade comprovada por mutação:** 12 de 12 mutantes detectados:
1. previsão promovida a fato;
2. conflito ignorado;
3. expiração ignorada;
4. gate sempre permite;
5. recalibração inverte a hipótese;
6. sem diferença-em-diferenças;
7. loop volta a comparar só o sinal;
8. decisões não persistem;
9. medição sem observação nova;
10. desconhecido vira zero;
11. correlação rotulada como causa;
12. histórico sem versão.

Mais o mutante "sem recalibração", também detectado (seção 8).

## 7. Separação FATO / INFERÊNCIA / HIPÓTESE / PREVISÃO (saída real, `evidencias_comando_2.py`)
```
- revenue: FATO OBSERVADO: revenue = 300.0
    FACT      300.0  fonte=finance_ledger  ref=finance_entries[channel_id=chan_1;n=2;...]
    FACT      300.0  fonte=sales_tracking  ref=sales_events[channel_id=chan_1;value_type=REAL;n=42;...]
    FORECAST  900.0  base='eventos FORECAST lançados no rastreamento'
    INFERENCE 150.0  base='eventos ESTIMATED lançados no rastreamento'
- margin: FATO OBSERVADO: margin = 180.0  (receita − custos realizados do mesmo livro)
- audience_size: DESCONHECIDO: não há registro sobre audience_size
- revenue chan_2: CONFLITO — finance_ledger=300.0; sales_tracking=450.0 → decisão BLOCKED
```
As hipóteses do ciclo são gravadas como HYPOTHESIS e as expectativas como FORECAST. Nenhuma dessas classes é devolvida como `fact_value`.

As classes ESTIMATE e UNKNOWN **não são armazenáveis**: estimativa entra como INFERENCE com base declarada, e desconhecido é ausência de registro.

## 8. Prova do ciclo adaptativo (saída real)
```
rodada 1: [controlado] previsto=0.0500 real(DiD)=0.0350 erro=0.0150 | [refutável] previsto=0.0600 real=0.0250 dir_ok=False
rodada 3: [controlado] previsto=0.0500 real(DiD)=0.0425 erro=0.0075 | [refutável] previsto=0.0400 real=0.0175 dir_ok=False
rodada 5: [controlado] previsto=0.0500 real(DiD)=0.0450 erro=0.0050 | [refutável] previsto=0.0360 real=0.0150 dir_ok=False
- novo gancho eleva a conversão: VALIDATED / CAUSAL_SUPPORTED → adaptar: SCALE
- thumbnail escura eleva a conversão: ABANDONED / REFUTED (acerto de direção 0%) → adaptar: ABANDON
- Owner propõe escalar a refutada → DISAGREED ("a realidade REFUTOU esta estratégia")
```

**Recalibração comprovada:** sem ela, a previsão cresce de 0,050 para 0,074 e o erro **sobe** de 0,015 para 0,029. Com ela, a previsão converge para 0,050, que é a taxa real dos eventos novos, e o erro **cai** de 0,015 para 0,005. O teste dedicado reprova o mutante sem recalibração.

**Correlação × causa:** com os mesmos dados reais e uma tendência comum a todos os grupos, o desenho sem controle chega a `CORRELATIONAL_ONLY` (recomendação `TEST_WITH_CONTROL`), e o desenho com controle chega a `REFUTED`.

**Memória:** após restart, uma nova sessão recupera a decisão `BLOCKED` e as duas estratégias com seus status.

## 9. Pendências (nenhuma bloqueadora)
1. **Linguagem natural:** a `AtlasConsultation` garante o conteúdo (classes, evidência, incerteza, continuidade). A redação em linguagem natural por LLM **não foi validada**, porque não há rede nem provedor real neste ambiente. Não afeta a correção do estado nem das decisões.
2. **Aprendizado legado:** o `LearningLoop` comercial continua volátil e apenas informativo. Nenhuma decisão nova depende dele.
3. **Janelas de validade:** os TTLs (cadastro 7 d, desempenho 24 h, integração 1 h, concorrência 3 d sem validade própria) são constantes documentadas e podem ser calibradas com o uso real.
4. **Revalidação no Windows:** recomendável para o arquivo novo de testes. Ele não depende de SO, e nenhuma linha do código validado no Windows foi alterada.
