# CRM V1

`commercial/crm.py` — Seção 47.

Lifecycle: `NEW → ENGAGED → QUALIFIED → OFFERED → CUSTOMER | LOST | UNSUBSCRIBED`.

Transições são validadas por uma máquina de estados explícita (`_VALID_TRANSITIONS`) — pular etapas (ex: `NEW → CUSTOMER` direto) levanta `InvalidLeadTransition`. `LOST` e `UNSUBSCRIBED` são estados terminais.
