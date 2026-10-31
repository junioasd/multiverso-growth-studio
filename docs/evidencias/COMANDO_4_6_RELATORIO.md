# COMANDO MESTRE 4/6 — RADAR WEB COMERCIAL + VISÃO EVOLUTIVA + MULTIMODAL + ALL-ROUNDER + AGENTIC-FIRST

**Status: CONCLUÍDO (com as limitações de validação externa declaradas na seção 8).**
**Baseline imutável:** `multiverso_growth_studio_comando_3_6_final_2026-09-24.zip` (SHA-256 `f4a6468b173044d04008af7ce8ff016f124d528bbfe8c2b621aeef62ffb033d2`).

## 1. Resultado
| Testes | PASS | FAIL | ERROR | SKIP | RC | ResourceWarning |
|---|---|---|---|---|---|---|
| 1.990 | 1.988 | 0 | 0 | 2 | 0 | 0 |

- Os 1.990 testes somam os 1.963 do baseline e 27 novos em `tests/test_comando_4_radar_evolutivo_agentic.py`.
- Os 2 skips são `test_real_windows_*`, aprovados em Windows real no Comando 1.
- **Mutação:** 16 de 16 mutantes detectados.
- **Comandos 1 e 3 intactos:** todos os seus arquivos são idênticos, byte a byte, ao baseline.
- **Comando 2 estendido só com acréscimos** (`digital_twin.py`, `learning.py`, `director.py`). A única linha "removida" em `learning.py` foi reescrita para incluir `REVALIDATE` entre os status DECIDED. Os 32 testes do Comando 2 seguem verdes.

## 2. Estado encontrado (já existia — reutilizado)
- Radares (`atlas.radar`, `radar_web`), descoberta com proveniência e validade, pipeline de adoção com quality gate e score transparente (`FunnelIntelligenceScore`).
- Camada de evidência multimodal com analisadores reais: web, PDF, DOCX, imagem, áudio e vídeo.
- 25 agentes ativos com `RealAgentContract`; seleção por competência em 4 níveis (`resolve_agent_for_task`).
- Gêmeo Digital, ciclo adaptativo, NAVIGATOR/FORGE e `JobQueue` dos Comandos 2 e 3.

## 3. Lacunas provadas (antes de qualquer alteração)
1. **Mudança de regime:** depois que a conversão real caiu de 4,5% para 1,4%, o ATLAS **continuava recomendando ESCALAR** uma estratégia validada no regime anterior.
2. **Orquestração agêntica:**
   - nenhum código executava o agente selecionado, validava o retorno, redelegava ou limitava tentativas;
   - `AgentTaskResult.completed` aceita **nenhuma evidência** com confiança padrão de **1.0**, e o `PRISMA` real devolve exatamente isso;
   - `COMPLETED` com `INSUFFICIENT_DATA` interno só era barrado pela confiança.
3. **Sinais comerciais multimodais:** os analisadores extraem apenas propriedades medidas (correto como fato). Não havia extração comercial, e o texto extraído era descartado.
4. **Visão Evolutiva sobre dados de mercado:** não havia detecção de mudança sobre o histórico factual.
5. **RADAR contínuo:** a observação de páginas não era agendada.

## 4. Arquitetura final
```
RADAR (radar_observe_page: RealWebPageAnalyzer → busca real)
  → commercial_signals (FATO literal c/ citação+posição+hash | INFERÊNCIA | DESCONHECIDO)
  → Gêmeo Digital (classe preservada; UNKNOWN nunca gravado; validade/versão/conflito)
  → detect_changes (PERSISTENTE | PONTUAL | NÃO CONFIRMADA; só FATO; associação, não causa)
  → ciclo adaptativo (HOLD / REVALIDATE / NEEDS_REVALIDATION; ABANDON de refutadas)
  → radar_web.intelligence (brief com todos os campos + dados faltantes; priorização explicada;
                            conflito/insuficiência não ranqueiam; histórico penaliza)
  → NAVIGATOR (pré-condição factual do Gêmeo bloqueia o plano sem fato vigente)
ATLAS.agentic: especialista → ALL-ROUNDER; payload isolado; rejeita sem evidência, com dados insuficientes
               ou exercendo autoridade; redelega; bloqueia laço e profundidade; orçamento de tentativas e tempo;
               auditoria; conclusão = INFERÊNCIA
Contínuo: JobQueue (radar_observe_page por tick, reagendado)
```

## 5. Arquivos
- **Novos:**
  - `atlas/agentic.py`
  - `core/multimodal/commercial_signals.py`
  - `radar_web/intelligence.py`
  - `tests/test_comando_4_radar_evolutivo_agentic.py`
  - este relatório
- **Estendidos:**
  - `atlas/digital_twin.py` (`detect_changes`)
  - `atlas/learning.py` (regime)
  - `atlas/director.py` (`agentic`, `radar_observe_page`)
  - `core/multimodal/evidence.py` (`content_text`)
  - `real_webpage_analyzer.py` (hash e texto)
  - `real_document_analyzer.py` (texto)

## 6. Provas (tudo pelo app real; busca HTTP real em loopback; PNG real)
- **Proveniência:** cada sinal FATO cita o trecho exato (`content_text[span] == quote`), o `evidence_id` e o hash. O Gêmeo guarda fonte e referência.
- **Freshness:** dado vencido fica `EXPIRED` e o gate bloqueia.
- **Ausência:** gera `UNKNOWN` / `INSUFFICIENT_EVIDENCE`, com os campos faltantes listados.
- **Fonte fraca:** vira `LOW_CONFIDENCE`, inutilizável para decisão.
- **Conflito:** duas páginas com preços diferentes geram `CONFLICT`. O brief fica `BLOCKED_CONFLICT`, não é ranqueado, e a decisão crítica é `BLOCKED`.
- **Evolução:** R$ 97 → 147, confirmado por uma observação seguinte, dá `PERSISTENT` (quando, quanto, fonte). Uma única observação dá `UNCONFIRMED`, e a tendência não sustentada não é tratada como mudança. Uma ida e volta dá `TRANSIENT`. Inferência nunca gera mudança.
- **Multimodal:**
  - CTA, garantia e preço literais são FATO; promessa, preço da oferta, ângulo e inconsistência são INFERÊNCIA;
  - preços múltiplos (de/por, parcelas) ficam `UNKNOWN` e nada vai para o Gêmeo;
  - imagem real tem as propriedades medidas como FATO e a semântica visual como `UNKNOWN`.
- **Agentic:**
  - especialista `PRISMA` escolhido, resultado INFERÊNCIA;
  - agente sem evidência é rejeitado e o `PRISMA` é aceito na redelegação;
  - dados insuficientes rejeitados; payload isolado;
  - agente tentando aprovar ou decidir é rejeitado, e um principal de agente não consegue aprovar escalação;
  - laço é bloqueado; orçamentos de tentativa e de tempo funcionam; tudo é auditado.
- **ALL-ROUNDER:** desempenho e correlação vêm do `PRISMA` real; a perspectiva de público (SAGE, sem provedor) aparece como **ausente**, nunca inventada.
- **Estratégia:** o brief nunca nasce VALIDADO; o ranking mostra a origem e a contribuição de cada critério; critério sem origem é recusado.
- **Abandono:** hipótese refutada gera `ABANDON` e penaliza a prioridade.
- **Regime:** `SCALE` → `HOLD` (1 observação) → `REVALIDATE` / `NEEDS_REVALIDATION` (persistente), e isso sobrevive a restart.
- **Integração RADAR → ATLAS → Gêmeo → NAVIGATOR:** com fato vigente, o plano não fica bloqueado; depois que o preço fica ambíguo, o plano é bloqueado.
- **Contínuo:** ticks via `JobQueue` constroem uma série real e detectam a mudança persistente. Fonte fora do ar não grava nada nem apaga o estado.
- **Guarda de rede:** atendida com um `patch` de `urlopen` que só permite loopback e recusa host externo (há teste provando a recusa). A guarda não foi alterada.

## 7. Defeitos de teste corrigidos durante o trabalho (sem afrouxar)
- Séries de 2 pontos: o `PRISMA` real exige 3. O coordenador rejeitou corretamente, e os dados do teste foram corrigidos.
- Assinatura e tipo de ação de `request_critical_action` estavam errados no teste.
- Uma linha de teste sem sentido (asserção vazia) foi removida antes da execução.

## 8. Limitações de validação externa (dependem da operação real)
- **Nenhuma coleta de mercado externa ocorreu:** não há rede neste ambiente. Toda busca foi contra servidor HTTP local real. Nenhum resultado de mercado real foi declarado.
- **Semântica visual** (o que uma imagem ou thumbnail mostra) depende de um modelo de visão, que não está configurado. Hoje é sempre `UNKNOWN`.
- **Agentes de pesquisa** (SAGE, VERITAS, ORION…) dependem de provedores externos. Sem eles, devolvem `UNAVAILABLE` e o ATLAS escala.
- **Limiares** (25% para mudança relevante, 1 confirmação para persistência, TTL de 3 dias para concorrência) são constantes documentadas e devem ser calibradas com dados reais.
- Recomenda-se revalidar os testes novos no Windows.
