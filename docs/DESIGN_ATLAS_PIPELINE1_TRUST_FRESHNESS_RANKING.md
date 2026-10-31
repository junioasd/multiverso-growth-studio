# DESIGN — ATLAS PIPELINE 1: FRONTEIRA DE CONFIANÇA, FRESHNESS, RANKING

Escrito ANTES da implementação, conforme exigido. Complementa
`DESIGN_ATLAS_PIPELINE1_DISCOVERY.md`.

## 1. Fronteira de confiança (trust boundary)

### Problema
`has_valid_provenance()` (rodada anterior) valida apenas a
consistência/completude dos campos que o PRÓPRIO adapter declara —
um adapter pode, sozinho, "criar" prova suficiente para REAL/FACT só
preenchendo os campos certos, sem nenhuma chamada externa real.

### Modelo
Duas entidades NOVAS, controladas pela INFRAESTRUTURA
(`ContentDiscoveryEngine`), nunca pelo adapter:

- `SourceRegistry` — quais `source_id` são conhecidos/confiáveis
  (registrados no momento em que o engine é construído, a partir da
  lista de fontes que o CHAMADOR forneceu — nunca autodeclarado pelo
  próprio adapter em tempo de execução) e se são `is_test_source`
  (marcados explicitamente como fonte de teste/simulação).
- `ReceiptRegistry` + `SourceExecutionReceipt` — um comprovante
  EMITIDO pelo engine no INSTANTE em que decide chamar
  `source.discover()` (nunca antes, nunca pelo adapter), com um
  `execution_id` novo a cada chamada. O comprovante só é marcado
  `COMPLETED` DEPOIS que a chamada retorna sem exceção — se
  levantar, vira `FAILED` e nenhum sinal daquela chamada pode ser
  validado.

### Onde a validação acontece: na FRONTEIRA de descoberta, não no ranking
`scan_source()` (não `rank_all()`) é o único lugar que:
1. Confirma que a fonte está registrada — senão, indisponível.
2. Emite o receipt ANTES de chamar `discover()`.
3. Passa `receipt_id` ao adapter (opcional — um adapter que não
   referencia o receipt em seus sinais nunca produz REAL/FACT).
4. Após a chamada retornar (sem exceção), marca o receipt
   `COMPLETED`.
5. SANITIZA cada sinal de cada oportunidade retornada: um sinal com
   `evidence != UNKNOWN` cujo `receipt_id` não corresponde a um
   receipt `COMPLETED`, da MESMA fonte, da MESMA execução — é
   REBAIXADO para `evidence=UNKNOWN`/`claim_type=UNKNOWN`/
   `confidence=0.0` ANTES de ser persistido. Nunca remove a
   oportunidade, nunca fabrica um valor — só torna a evidência
   HONESTA quando não pôde ser verificada.
6. Se a fonte é `is_test_source` e o engine não foi explicitamente
   configurado com `allow_test_sources=True`, TODOS os sinais são
   igualmente rebaixados, mesmo com receipt válido.

Por que sanitizar na fronteira (não checar o receipt de novo no
`rank_all()`): o `ReceiptRegistry` é efêmero, em memória, escopado à
execução de UM `scan_source()` — não sobrevive a um restart nem a uma
retomada em outro processo (mesma disciplina do resto do projeto: só
o `OpportunityStore`, SQLite, é a fonte de verdade persistente). Ao
sanitizar ANTES de persistir, o `rank_all()` (que pode rodar bem mais
tarde, em outro processo) nunca precisa reconstruir/reconsultar
receipts — a evidência já persistida já reflete honestamente o que
foi verificado.

### O que isto NÃO promete
Não é prova criptográfica de que um efeito externo genuíno ocorreu —
um adapter em processo, com acesso total à mesma memória, ainda
poderia, em teoria, forjar um `receipt_id` correto se tivesse acesso
interno ao registro do engine. A fronteira é HONESTA, não
inquebrável: eleva substancialmente a barra (exige um comprovante
genuíno emitido pela infraestrutura, não apenas alguns campos
"bonitos"), nunca finge uma garantia que não pode cumprir.

## 2. Freshness — `valid_until=None` nunca é validade infinita

### Modelo
Nova `DiscoverySignal.is_fresh(now=None, max_age_seconds=DEFAULT)`:
- Se `valid_until` está definido: vigente enquanto `now <=
  valid_until` (fonte mais confiável de vigência — inalterado).
- Se `valid_until` é `None` (desconhecido): vigente enquanto `now -
  observed_at <= max_age_seconds` — uma política de freshness PADRÃO
  e DOCUMENTADA (`DEFAULT_FRESHNESS_WINDOW_SECONDS = 24h`, razoável
  para sinais de tendência de conteúdo, que mudam rápido) — NUNCA
  eterno.

`_status_for()`/`_score()` passam a usar `is_fresh()` (não mais
`is_expired()` isoladamente) para decidir o que conta como atual.

## 3. Ranking — normalização, pesos, nunca soma direta de unidades diferentes

### Modelo
Cada componente é NORMALIZADO para `[0, 1]` antes de combinar —
documentado explicitamente:

| Sinal | Direção | Normalização |
|---|---|---|
| `trend_strength` | quanto maior, melhor | já `[0,1]`, usado direto (com clamp defensivo) |
| `hook_strength` | quanto maior, melhor | já `[0,1]`, usado direto |
| `estimated_reach` | quanto maior, melhor | `min(1, valor / 1_000_000)` — referência documentada |
| `competition` | quanto maior, PIOR | invertido: `1 - valor` |
| `risk` | quanto maior, PIOR | invertido: `1 - valor` |
| `production_cost` | quanto maior, PIOR | invertido e normalizado: `1 - min(1, valor / 100_000)` |

Combinados por MÉDIA PONDERADA (pesos explícitos e documentados no
código) — nunca soma bruta. `trend_strength` é OBRIGATÓRIO (sem ele,
nunca um score, mesmo com outros componentes presentes — é o único
sinal verdadeiramente essencial para "existe alguma tendência aqui").
Componentes AUSENTES são EXCLUÍDOS da média (nunca um valor favorável
inventado — risco/custo ausentes nunca contam como "zero risco/custo
zero").

`evidence_quality`: o score final é multiplicado pela confiança MÉDIA
dos componentes usados — baixa confiança reduz o score explicitamente
(nunca ignorada). Faixa final: sempre `[0, 1]`, por construção (média
ponderada de termos em `[0,1]`, multiplicada por confiança em
`(0,1]`).

Sinal `risk` (numérico, por oportunidade) é DISTINTO de `risk_level`
(parâmetro externo do `rank_all()`, ex: de uma política/classificação
— já existente) — os dois continuam coexistindo: `risk_level="HIGH"`
continua BLOQUEANDO via o gate (hard block, inalterado); o sinal
`risk` numérico agora TAMBÉM reduz o score continuamente (soft
penalty), mesmo quando não classificado como "HIGH".
