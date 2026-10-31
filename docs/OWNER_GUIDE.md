# Owner Guide

## Rodando localmente

```bash
cd multiverso_growth_studio
cp .env.example .env   # preencher OWNER_EMAIL e (via comando abaixo) OWNER_PASSWORD_HASH
python3 main.py set-owner-password   # gera o hash e mostra a linha para o .env
python3 main.py health
python3 main.py serve --host 127.0.0.1 --port 8787
# Login:     http://127.0.0.1:8787/login
# Dashboard: http://127.0.0.1:8787/dashboard
```

**O `.env` é carregado automaticamente** por `core/config.py` antes de
qualquer serviço ser construído — não é preciso exportar as variáveis
manualmente no shell. O arquivo é procurado primeiro no diretório de
trabalho atual, depois na raiz do projeto. Uma variável já definida no
ambiente real (`export OWNER_EMAIL=...`) sempre tem precedência sobre o
`.env`. O conteúdo é tratado como dado literal — nunca executado nem
expandido (hashes com `$` são preservados exatamente como estão).

## Migração de banco (mudança de schema entre versões)

O schema é aplicado com `CREATE TABLE IF NOT EXISTS` — rodar
`python3 main.py health` (ou qualquer comando que chame `build_app()`)
sobre um banco de uma versão anterior apenas ADICIONA as tabelas novas
que faltarem, sem tocar nas existentes. Nenhum dado é perdido ao
atualizar para uma versão mais recente do MULTIVERSO GROWTH STUDIO.

## Gerando um hash de senha para o Owner (alternativa via API Python)

```python
from core.security.auth import hash_password
print(hash_password("sua-senha-forte-aqui"))
```

Copie o resultado para `OWNER_PASSWORD_HASH` no seu `.env` real (nunca no `.env.example`, nunca commitado).

## Persistência

Por padrão, `build_app()` conecta todos os serviços mutáveis (CRM,
vendas, finanças, jobs, auditoria) a um arquivo SQLite (`DB_PATH` no
`.env`, padrão `./data/mgs.db`). O estado sobrevive a reinício do
processo.

## Rodando os testes

```bash
python3 -m unittest discover -s tests -v
```

## Estrutura para o Piloto (Seção 65)

1 canal + 1 produto/oferta + conteúdo + vídeo de alta qualidade + distribuição orgânica + funil + tracking + vendas reais quando ocorrerem — depois expandir com evidência.

## Limites desta fase (fundação)

- Nenhuma integração paga real está ativa (tudo em modo MOCK até credenciais serem configuradas pelo Owner).
- Nenhuma publicação real acontece sem OAuth explicitamente concluído pelo Owner.
- Build state: `IMPLEMENTED + TESTED — PENDING EXTERNAL AUDIT`. Não avançar para produção real antes da auditoria.
