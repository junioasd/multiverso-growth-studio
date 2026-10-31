# MULTIVERSO GROWTH STUDIO — Arquitetura (V1 / Fundação)

Projeto novo e separado do MULTIVERSO DIGITAL IA CORE. Owner: Júnior.

## Camadas

```
core/               fundação técnica (segurança, isolamento, jobs, quality, produção, adapters)
  security/          auth, RBAC, audit, sanitize
  models/            entidades de domínio + schema SQLite
  isolation/          Project/Channel Isolation
  jobs/                Job Queue (idempotência, retry, circuit breaker)
  quality/              Quality Gates genéricos, de vídeo e comerciais
  production/            Production Engine (roteiro -> narração -> cenas -> ... -> preview)
  adapters/                Video Router, Voice Engine, Music Engine, Platform Adapters, Affiliate Adapters

commercial/         MultiGerenciador Comercial e módulos de vendas
  multigerenciador.py    orquestrador: mercado -> produto -> oferta -> funil -> venda -> aprendizado
  affiliate_marketplace_intelligence.py
  product_scoring.py
  affiliate_compliance.py
  funnel_engine.py
  crm.py
  sales_tracking.py
  finance.py
  experiment_engine.py
  learning_loop.py

youtube_strategy/   YouTube Strategy Engine, Alignment Checker, Change Radar

api/                Owner Experience: bootstrap (composição), server (login/dashboard), dashboard/war room

tests/               suíte real (unittest, stdlib) cobrindo Seções 67-70
```

## Princípios estruturais (do Comando Mestre)

1. **Separação do CORE**: nenhuma dependência estrutural rígida com MULTIVERSO DIGITAL IA CORE, Claude, OpenAI, ou um único fornecedor.
2. **Provider lock-in**: todo provider externo (vídeo, voz, música, plataforma, afiliados) passa por um adapter trocável. Sem credencial configurada → modo MOCK explícito, nunca simula execução real.
3. **Nenhum segredo hardcoded**: tudo vem de variáveis de ambiente (`.env`, nunca commitado). `.env.example` contém apenas placeholders.
4. **Isolamento total**: Channel Isolation e Project Isolation aplicados via `core.isolation.IsolationGuard` — nunca implícito.
5. **Quality Gate transversal**: nenhum avanço sem aprovação explícita (`APPROVED`), com `UNKNOWN` sempre que faltar dado — nunca inventado.
6. **Owner é a única autoridade humana**: nenhuma IA/agente é Owner; RBAC aplica o princípio de menor privilégio.
7. **Nunca publica sozinho**: a pipeline de produção sempre termina em `PREVIEW` / `AWAITING_OWNER_AUTHORIZATION`.

## Estado atual (build state)

`MULTIVERSO GROWTH STUDIO — FOUNDATION IMPLEMENTED + TESTED — PENDING EXTERNAL AUDIT`

Ver `docs/OWNER_GUIDE.md` para como rodar localmente e `docs/SECURITY.md` para o modelo de ameaça e testes adversariais.
