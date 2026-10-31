# Copyright Compliance

`core/production/copyright_gate.py` — Seção 20.

| license_status | resultado |
|---|---|
| OWN_ASSET | aprovado |
| LICENSED_LIBRARY | aprovado |
| CLEARED_COMMERCIAL | aprovado |
| UNKNOWN | **BLOCK** |
| AMBIGUOUS | **BLOCK** |
| INCOMPATIBLE | **BLOCK** |

Mesmo com `license_status` aprovado, `commercial_use=False` ou `monetization_compatible=False` bloqueiam o ativo individualmente. Todo asset é avaliado — um único ativo problemático bloqueia o `CopyrightGateResult.approved`.
