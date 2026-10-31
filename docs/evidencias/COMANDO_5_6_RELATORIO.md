# COMANDO MESTRE 5/6 — MÁQUINA COMERCIAL PONTA A PONTA

**Status: CONCLUÍDO — LÓGICA VALIDADA LOCALMENTE. A operação externa real ainda NÃO foi validada (seção 8).**
**Baseline:** `multiverso_growth_studio_comando_4_6_final_2026-09-24.zip`, verificado contra o `SHA256SUMS.txt` do Comando 4 (`fb4e80ec…7ca9`).

## 1. Resultado
| Testes | PASS | FAIL | ERROR | SKIP | RC | ResourceWarning |
|---|---|---|---|---|---|---|
| 2.009 | 2.007 | 0 | 0 | 2 | 0 | 0 |

- Os 2.009 testes somam os 1.990 do baseline e 19 novos em `tests/test_comando_5_maquina_comercial.py`.
- Os 2 skips são `test_real_windows_*`, aprovados em Windows real no Comando 1.
- **Mutação:** 16 de 16 mutantes detectados. O runner estrito de recursos termina com RC 0.
- **Comandos 1, 3 e 4:** arquivos idênticos, byte a byte, ao baseline.
- **Comando 2:** a única mudança foi em `atlas/digital_twin.py`, onde a margem sincronizada passou a descontar reembolsos (correção da lacuna 3). Os 32 testes do Comando 2 seguem verdes.

## 2. Estado encontrado (já existia — reutilizado, não duplicado)
- CRM com ciclo de vida do lead: NEW → ENGAGED → QUALIFIED → OFFERED → CUSTOMER.
- `SalesTracker`: eventos impression/view/click/lead/checkout/sale/refund, com 6 dimensões e `ValueType`.
- `FinanceLedger`, `MultiGerenciadorComercial`, `OmnichannelIntelligence` (orgânico × ads), `FunnelEngine` e `ExperimentEngine`.
- `CommercialQualityGate` (margem ≤ 0 = REJECTED), `AffiliateComplianceGate` e `MasterLibrary` versionada.
- Motor de distribuição do YouTube, agentes METRICA, MERCURY, NEXUS e RELAY, e as camadas dos Comandos 1–4.

## 3. Lacunas provadas e fechadas
1. **Não havia pedido, pagamento nem entrega:** checkout e venda eram eventos atômicos, e não existiam PAYMENT_PENDING, PAID, FAILED ou DELIVERED. **Fechado** com `commercial/journey.py`.
2. **Atribuição inventada:**
   - `instagram_patrocinado`, `post_impulsionado`, `anuncio_meta` e `trafego_pago` eram classificados como ORGÂNICO;
   - `email`, `direto` e `indicacao` também viravam ORGÂNICO.
   - **Fechado:** marcadores de tráfego pago em português, e ORGÂNICO só com marcador explícito. O resto é UNKNOWN.
3. **Margem inflada:** o `FinanceLedger` não conhecia reembolso, e a margem do Gêmeo herdava o erro. **Fechado:** novo tipo `refund`, `net_revenue`, e margem = receita − reembolsos − custos.
4. **Biblioteca sem validade:** uma entrada VALIDATED governava para sempre, e não havia detecção de contradição. **Fechado:** `valid_until`, com migração aditiva, e `governing()` que devolve GOVERNING, EXPIRED, CONFLICT ou UNKNOWN.
5. **Sem registro de promessas comerciais:** o `ClaimRegistry` existente cobre evidência, não copy. **Fechado** com `commercial/claims.py`.

## 4. Arquitetura final
```
RADAR/ATLAS/Gêmeo (C2/C4) → hipótese de campanha (ciclo adaptativo)
 → NAVIGATOR/FORGE/Owner/Hub (C3): publicação CONFIRMED só com leitura real
 → distribution_ladder: PUBLISHED → DISTRIBUTED → VIEWED → ENGAGED → CONVERTED (só FATO vigente; sem fato = DESCONHECIDO)
 → CRM (lead) → OrderJourney: CHECKOUT_STARTED → PAYMENT_PENDING → PAID|FAILED → DELIVERED → POST_SALE → RETAINED; REFUNDED
     (cada evento de gateway processado 1×; lançamentos via GovernedFacade com idempotency_key)
 → FinanceLedger (receita/comissão/custo/reembolso) → unit_economics (margem, CAC, ROI, LTV observado; veredito)
 → Gêmeo (sync) → ciclo adaptativo (VALIDATED/ABANDONED/REVALIDATE) → Biblioteca Mestre (aprendizado com validade)
 → radar_web.intelligence (priorização penaliza refutadas)
CommercialClaimRegistry: copy só passa com claim APROVADO, vigente, no canal permitido e sem promessa proibida ou não registrada
```

## 5. Arquivos
- **Novos:**
  - `commercial/journey.py`
  - `commercial/claims.py`
  - `tests/test_comando_5_maquina_comercial.py`
  - este relatório
- **Alterados:**
  - `commercial/finance.py` (reembolso)
  - `commercial/omnichannel_intelligence.py` (atribuição)
  - `core/library/master_library.py` (validade e governança)
  - `atlas/digital_twin.py` (margem com reembolso)
  - `core/models/db.py` (tabelas `commercial_orders` e `commercial_claims`; coluna `valid_until`; tudo aditivo)

## 6. Provas (pelo app real)
- **Jornada:**
  - checkout e pagamento pendente **nunca** geram venda nem receita;
  - PAID exige evidência do gateway **e** o valor exato do pedido;
  - entrega só a partir de PAID e com evidência;
  - pagamento falho e carrinho abandonado viram candidatos de recuperação, e a nova tentativa de pagamento funciona;
  - RETAINED só com **recompra paga** (um checkout de recompra não conta);
  - o estado sobrevive a restart.
- **Idempotência:** 3 repetições de pagamento, entrega, reembolso e checkout resultam em exatamente 1 venda, 1 reembolso, 1 checkout e 1 lançamento de cada tipo, inclusive com retry depois do restart.
- **Finanças:**
  - receita 400, reembolso 200, comissão 120 e custo 40 dão margem **40**, no livro e no Gêmeo;
  - faturamento de 200 com anúncio de 300 dá `REVENUE_WITHOUT_MARGIN`, **não é sucesso**;
  - CAC 150, conversão 2% e LTV observado 100;
  - entradas desconhecidas resultam em UNKNOWN; CAC sem origem declarada é recusado.
- **Atribuição:** marcadores pagos em português → PAID_ADS; fontes sem base → UNKNOWN; venda sem origem não recebe atribuição.
- **Claims:**
  - claim com fato vigente é aprovado só no canal permitido;
  - claim baseado em hipótese é bloqueado ("copy não transforma hipótese em fato");
  - claim sem evidência, promessa proibida, promessa quantitativa não registrada e claim ou evidência vencidos são bloqueados.
- **Biblioteca:** entrada vencida resulta em EXPIRED; duas entradas vigentes contraditórias resultam em CONFLICT, e nenhuma governa; a validade persiste.
- **Distribuição:** a publicação exige aprovação do Owner. Resposta "processing" fica EXECUTED, não publicado. Sem confirmação real, nenhum degrau é afirmado. Sem venda, CONVERTED é DESCONHECIDO (e não zero).
- **Ponta a ponta:**
  1. hipótese testada com grupo de controle;
  2. publicação CONFIRMED;
  3. 25 pedidos pagos e entregues;
  4. `ECONOMIC_SUCCESS`, estratégia VALIDATED e recomendação SCALE;
  5. aprendizado registrado na Biblioteca com validade (vencido, exige reteste).
- **Estratégia ruim:** passa a ABANDONED, a recomendação vira ABANDON e a prioridade é penalizada.

## 7. Defeitos de teste corrigidos antes da execução (sem afrouxar)
- Duas chamadas condicionais (`if hasattr`) que poderiam pular verificações em silêncio foram removidas.
- `validate()` exige `validated_by`; a chamada foi corrigida.

## 8. OPERAÇÃO EXTERNA REAL AINDA NÃO VALIDADA
- **Gateways de pagamento** (webhooks reais, assinatura, chargeback): a jornada recebe os eventos, mas nenhum gateway real foi conectado.
- **Plataformas de publicação e contas de anúncio:** exercitadas só contra servidor HTTP local real.
- **Entrega física ou digital real, suporte e satisfação:** representados por evidência declarada no evento; não há integração real.
- **Atribuição multitoque / efeito assistido entre canais:** exige dados reais de caminho do usuário. Hoje o sistema **recusa** atribuir sem base (UNKNOWN), e não modela multitoque.
- **Ações de recuperação** (mensagem, cupom): identificadas pela jornada; a execução passa pelo NAVIGATOR com aprovação do Owner (Comando 3), sem canal real conectado.
- Recomenda-se revalidar os testes novos no Windows.
