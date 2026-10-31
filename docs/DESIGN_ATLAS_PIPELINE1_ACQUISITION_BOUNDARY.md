# DESIGN — ATLAS PIPELINE 1: FRONTEIRA DE AQUISIÇÃO REAL

Escrito ANTES da implementação. Complementa os dois design docs
anteriores.

## Por que o receipt anterior não provava aquisição

O `SourceExecutionReceipt` (rodada anterior) era emitido ANTES de
`discover()` ser chamado e marcado `COMPLETED` só porque a função
RETORNOU — nunca provava que uma aquisição externa genuína ocorreu,
só que "a chamada não levantou". Um adapter podia copiar o
`receipt_id` recebido para sinais inteiramente inventados.

## Modelo novo: camada de TRANSPORTE controlada

O adapter NUNCA mais recebe um comprovante pronto para anexar aos
seus sinais. Recebe um `SourceExecutionContext` que só permite
SOLICITAR uma aquisição através de um `SourceAcquisitionBroker` —
este, sim, controlado pela infraestrutura:

```
engine cria execution_id
  -> passa um SourceExecutionContext (source_id + execution_id + broker) ao adapter
  -> adapter chama context.acquire(request=...)
    -> broker consulta o TRANSPORTE registrado para aquele source_id
       (registrado PELA INFRAESTRUTURA, nunca autodeclarado)
    -> SEM transporte registrado (ou com NotImplementedTransport, o
       padrão): levanta -- honesto, "não implementado"
    -> COM transporte real: fetch() de verdade acontece; só ENTÃO um
       AcquisitionResult é construído (acquisition_id, source_id,
       execution_id, timestamps, status, mode REAL/TEST, digest do
       resultado)
  -> adapter transforma o resultado em sinais, referenciando
     `acquisition_id`
  -> engine, ao sanitizar (scan_source(), antes de persistir), só
     aceita um sinal REAL/FACT se seu `acquisition_id` corresponder a
     um `AcquisitionResult` GENUÍNO desta fonte+execução, com
     `mode == "REAL"` (ou "TEST" só quando `allow_test_sources=True`)
```

Um adapter que apenas devolve números sem chamar `context.acquire()`
nunca obtém um `acquisition_id` genuíno — qualquer coisa que ele
"invente" para esse campo não corresponde a nada no
`SourceAcquisitionBroker`, e é sanitizada para `UNKNOWN`.

## Por que nenhuma fonte fica "confiável" automaticamente

Antes, TODA fonte passada em `sources=[...]` virava confiável
(`SourceRegistry.register(source.source_name, ...)` no construtor).
Corrigido: `ContentDiscoveryEngine` registra, por padrão, um
`NotImplementedTransport` (sempre levanta) para CADA fonte — a menos
que a infraestrutura EXPLICITAMENTE registre um transporte diferente
no broker. Presença na lista `sources=[...]` nunca, por si só,
concede confiança operacional.

## Modo de teste

`AcquisitionTransport.mode` é `"REAL"` ou `"TEST"` — controlado por
QUAL classe de transporte a infraestrutura registrou, nunca por um
atributo autodeclarado no adapter. Um `AcquisitionResult` com
`mode="TEST"` só é aceito pelo motor quando o engine foi
explicitamente construído com `allow_test_sources=True` — uso
exclusivo de teste determinístico, nunca operacional.

## Threat model — declarado honestamente

Esta arquitetura impede um adapter defeituoso ou customizado de
fabricar prova de aquisição apenas devolvendo sinais "bonitos" — a
prova só existe se um TRANSPORTE genuinamente registrado pela
infraestrutura foi de fato invocado. NÃO promete isolamento contra
código Python hostil com acesso arbitrário ao mesmo processo (ex:
introspecção de pilha para alcançar o broker diretamente) — isso
exigiria isolamento real de processo/sandboxing, fora de escopo desta
correção.

## Vigência malformada — nunca derruba o ranking

`is_fresh()`/`is_expired()` passam a validar o TIPO de `valid_until`
(e `observed_at`) antes de comparar — `bool`, string, `NaN`,
`Infinity`, `None` mal-empregado nunca levantam `TypeError`; sempre
retornam `False`/tratam como expirado, fail-closed. `rank_all()`
também envolve CADA oportunidade individualmente em tratamento de
exceção — um dado genuinamente malformado nunca derruba o ranking
das demais.

## `evidence_quality` — removido, nunca um campo ignorado

`evidence_quality` existia em `DISCOVERY_SIGNAL_KEYS` mas nunca
influenciava o ranking — um campo presente no contrato mas
silenciosamente ignorado. A REPRESENTAÇÃO CANÔNICA de qualidade de
evidência já existe e já é usada: a confiança MÉDIA (`confidence`)
dos sinais que efetivamente contribuem para o score (já multiplicando
o resultado final, desde a correção anterior). Um campo `evidence_
quality` SEPARADO e autodeclarado pela fonte reintroduziria
exatamente o tipo de autodeclaração não verificável que esta rodada
inteira corrige — removido do modelo, documentado o porquê.
