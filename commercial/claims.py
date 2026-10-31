"""
commercial.claims
===================
COMANDO 5/6 — registro verificável de CLAIMS / PROMESSAS comerciais.

O `ClaimRegistry` existente (core.evidence) registra afirmações de
EVIDÊNCIA; não controlava as PROMESSAS usadas em copy: onde podem ser
usadas, até quando, com quais restrições. Aqui:
- APPROVED só com evidência que resolve para FATO vigente no Gêmeo;
  evidência INFERÊNCIA/HIPÓTESE/PREVISÃO => BLOCKED ("copy não transforma
  hipótese em fato");
- promessas PROIBIDAS (cura, renda/resultado garantido, sem risco...) são
  bloqueadas sempre;
- `check_copy` bloqueia copy com claim não aprovado, vencido, fora do
  canal permitido, proibido, ou promessa quantitativa NÃO registrada.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from core.evidence.claims import ClaimType
from core.models import db as db_module

_FORBIDDEN = re.compile(r"\b(cura|curar|renda garantida|resultado garantido|lucro garantido|sem risco|"
                        r"100% garantid[oa]|enrique[çc]a r[aá]pido|aprovado pela anvisa|milagr\w*)\b", re.I)
_QUANT_PROMISE = re.compile(r"\b(\d+[.,]?\d*\s?(?:kg|%|x|mil|reais|dias|semanas)|R\$\s?\d+)", re.I)


class CommercialClaimRegistry:
    def __init__(self, *, twin, db_path: str) -> None:
        self.twin, self.db_path = twin, db_path

    def register(self, *, text: str, evidence_fact_ids: list, channels: list, owner: str, valid_until: float,
                 restrictions: str = "", now: float | None = None) -> dict:
        now = time.time() if now is None else now
        status, reason = "APPROVED", "evidência factual vigente"
        if _FORBIDDEN.search(text):
            status, reason = "BLOCKED", f"promessa PROIBIDA: '{_FORBIDDEN.search(text).group(0)}'"
        elif not evidence_fact_ids:
            status, reason = "BLOCKED", "claim sem evidência"
        else:
            for ref in evidence_fact_ids:
                fact = self.twin.get(ref)
                if fact is None:
                    status, reason = "BLOCKED", f"evidência '{ref}' não existe no Gêmeo"
                    break
                if fact.claim_type != ClaimType.FACT:
                    status, reason = "BLOCKED", (f"evidência '{ref}' é {fact.claim_type.value} — copy não "
                                                 f"transforma hipótese/inferência/previsão em fato")
                    break
                if fact.is_expired(now):
                    status, reason = "BLOCKED", f"evidência '{ref}' VENCIDA"
                    break
        if valid_until <= now:
            status, reason = "BLOCKED", "validade do claim já expirada"
        claim = {"claim_id": f"clm_{uuid.uuid4().hex[:12]}", "text": text, "evidence_refs": list(evidence_fact_ids),
                 "channels": list(channels), "restrictions": restrictions, "owner": owner, "status": status,
                 "status_reason": reason, "valid_until": valid_until, "created_at": now}
        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO commercial_claims (claim_id, text, evidence_refs_json, channels_json, restrictions, owner, "
                "status, status_reason, valid_until, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (claim["claim_id"], text, json.dumps(claim["evidence_refs"]), json.dumps(claim["channels"]),
                 restrictions, owner, status, reason, valid_until, now))
        return claim

    def get(self, claim_id: str) -> dict | None:
        with db_module.connect(self.db_path) as conn:
            r = conn.execute("SELECT * FROM commercial_claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["evidence_refs"], d["channels"] = json.loads(d.pop("evidence_refs_json")), json.loads(d.pop("channels_json"))
        return d

    def check_copy(self, *, copy_text: str, channel: str, claim_ids: list, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        problems = []
        m = _FORBIDDEN.search(copy_text)
        if m:
            problems.append(f"promessa PROIBIDA no texto: '{m.group(0)}'")
        approved_texts = []
        for cid in claim_ids:
            c = self.get(cid)
            if c is None:
                problems.append(f"claim '{cid}' não registrado")
                continue
            if c["status"] != "APPROVED":
                problems.append(f"claim '{cid}' {c['status']}: {c['status_reason']}")
            elif now >= c["valid_until"]:
                problems.append(f"claim '{cid}' VENCIDO — revalidar evidência")
            elif channel not in c["channels"]:
                problems.append(f"claim '{cid}' não autorizado para o canal '{channel}'")
            elif any(self.twin.get(r) is None or self.twin.get(r).is_expired(now) for r in c["evidence_refs"]):
                problems.append(f"claim '{cid}': evidência não está mais vigente")
            else:
                approved_texts.append(c["text"].lower())
        for q in _QUANT_PROMISE.finditer(copy_text):
            if not any(q.group(0).lower() in t for t in approved_texts):
                problems.append(f"promessa quantitativa NÃO registrada: '{q.group(0)}'")
        return {"status": "BLOCKED" if problems else "APPROVED", "problems": problems, "channel": channel}
