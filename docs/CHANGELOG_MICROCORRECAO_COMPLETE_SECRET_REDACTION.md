# CHANGELOG — Microcorreção BF2: Complete Secret Redaction

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_DEEP_SANITIZATION`
(1541 testes). Corrige as 2 falhas reais reproduzidas pela auditoria,
preservando tudo já aprovado no BLOCO FINAL 2 — incluindo os 90 testes
de sanitização/Defensive Copy/Immutable State/Owner Gate/Anti-Replay/
SpendGuard das rodadas anteriores.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1567  PASS: 1567  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~79-83s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1567 testes** (1541 preservados + **26 testes novos**).

## Falha 1 — Chaves sensíveis incompletas

`_SECRET_KEY_PATTERNS` não reconhecia `authorization`,
`proxy-authorization`, `bearer`, e `session` sozinho (só
`session_secret` estava coberto) — um valor como
`{"authorization": "Bearer TOKEN_REAL"}` passava pela sanitização
inteiro, sem nenhuma redação.

**Corrigido**: padrão expandido para incluir todas as chaves mínimas
exigidas — `authorization`, `proxy-authorization`, `bearer`,
`session` (sozinho), além de `apikey` (sem separador),
`access_token`/`refresh_token` (explícitos), `set-cookie` (explícito)
— todas case-insensitive (já era `re.IGNORECASE`, preservado).

## Falha 2 — `sanitize_text()` com lógica de substituição quebrada

A versão anterior usava
`m.group(0).split(m.group(0)[-1])[0] + _REDACTED` como substituição —
isso removia apenas o **último caractere** do valor encontrado e
concatenava o marcador, deixando o RESTO do segredo completamente
visível (ex: `"password: minhasenha_super_secreta_123"` virava
`"password: minhasenha_super_secreta_12[REDACTED]"` — 27 dos 28
caracteres do segredo permaneciam expostos). Além disso, o padrão de
valor `[^\s,;]+` parava no primeiro espaço, então
`"Authorization: Bearer eyJ..."` nunca era redigido corretamente (a
palavra "Authorization" nem estava na lista de chaves do texto, e
mesmo se estivesse, só "Bearer" seria capturado como "valor",
deixando o token real intacto).

**Corrigido**: substituição agora é sempre `"chave: [REDACTED]"` — o
valor inteiro correspondido é descartado por completo, nunca
parcialmente preservado. O padrão de valor reconhece `"Bearer
<token>"` como uma unidade só (nunca separa o esquema do token). Um
padrão adicional cobre `Bearer <token>` mesmo SEM uma chave nomeada
antes (ex: header colado cru). Durante o desenvolvimento, encontrei e
corrigi um efeito colateral do meu próprio ajuste: o padrão de valor
inicialmente não parava em `&`, engolindo o restante de uma
querystring inteira (`"token=X&outro=normal"` viraria
`"token: [REDACTED]"`, perdendo `"&outro=normal"`) — corrigido
adicionando `&` aos delimitadores de parada.

## Validado com os cenários exatos reportados

- `authorization` em dict direto → redigido.
- `Authorization`/`AUTHORIZATION`/case misto → redigido
  (case-insensitive confirmado).
- `Bearer <token>` em texto livre, com e sem `Authorization:` antes →
  totalmente redigido, nenhum fragmento do token sobrevive.
- `token=`/`api_key=`/`password=`/`secret=` em querystring/texto →
  totalmente redigidos, dados não-sensíveis adjacentes preservados.
- Estruturas aninhadas (`authorization` dentro de `list[dict]`,
  `proxy-authorization` dentro de `tuple`) → redigidas.
- Testado em `context` E `evidence`.
- Testado em memória E SQLite.
- **Confirmado nos bytes brutos do dump do arquivo SQLite** que o
  valor real nunca é persistido — nem o token completo, nem
  `"Bearer <token>"` como string composta.
- Confirmado que a trilha interna (fonte de qualquer log futuro)
  nunca contém o segredo original.

## Preservação confirmada explicitamente

Reexecutei os 7 arquivos de teste de segurança das rodadas anteriores
do BLOCO FINAL 2 — **90 testes, todos passando sem alteração**,
incluindo um teste de integração dedicado
(`test_full_stack_with_authorization_header`) que confirma Defensive
Copy on Input + Deep Sanitization + Complete Secret Redaction +
Immutable State + SpendGuard funcionando juntos corretamente com um
header `authorization` real no fluxo.

## Arquivos alterados
- `core/security/sanitize.py` — `_SECRET_KEY_PATTERNS` expandido;
  `sanitize_text()` reescrito com substituição completa e
  reconhecimento de `Bearer <token>`

## Arquivos novos
- `tests/test_microcorrecao_complete_secret_redaction.py` (26 testes)

**Nenhuma funcionalidade já aprovada foi removida.** Assinatura de
`sanitize_dict()`/`sanitize_text()` permanece idêntica; a redação de
chaves já cobertas antes (`password`, `token`, `secret`, etc.)
continua funcionando exatamente como antes — apenas a cobertura de
chaves e a completude da redação em texto livre foram reforçadas.

## Pendências reais (honestamente declaradas)
- A heurística de valor sem chave nomeada associada
  (`_looks_like_secret_value`) permanece um stub reservado para o
  futuro — mesma limitação documentada nas correções anteriores; o
  escopo desta correção é sobre segredos identificados por NOME DE
  CHAVE ou pelo padrão `Bearer <token>`, que é o que a auditoria
  reportou e testou
- `pytest` continua indisponível neste ambiente
