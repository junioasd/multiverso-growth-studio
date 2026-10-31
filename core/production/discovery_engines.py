"""
core.production.discovery_engines
====================================
Title Engine, Thumbnail Engine, SEO Engine (Seção 26). Avaliam clareza,
curiosidade, promessa, coerência, mobile visibility, originalidade,
search intent, semantic relevance, CTR potencial. Sem clickbait
enganoso.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CLICKBAIT_PATTERNS = [
    r"\bvocê não vai acreditar\b",
    r"\bo médico não quer que você saiba\b",
    r"\bisso vai mudar sua vida\b",
    r"\bninguém está falando sobre isso\b",
    r"\$\$\$",
]

MAX_TITLE_LENGTH_MOBILE = 60  # caracteres — referência para não truncar em mobile


@dataclass
class TitleAssessment:
    score: float
    issues: list[str] = field(default_factory=list)
    misleading: bool = False


class TitleEngine:
    def evaluate(self, title: str, *, promised_content_summary: str) -> TitleAssessment:
        issues: list[str] = []
        score = 1.0

        if len(title) > MAX_TITLE_LENGTH_MOBILE:
            issues.append(f"título pode truncar em mobile (>{MAX_TITLE_LENGTH_MOBILE} caracteres)")
            score -= 0.15

        misleading = False
        for pattern in _CLICKBAIT_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                issues.append(f"padrão de clickbait enganoso detectado: '{pattern}'")
                misleading = True
                score -= 0.3

        # heurística simples de coerência: alguma palavra do título deve
        # aparecer no resumo do conteúdo prometido
        title_words = {w.lower() for w in re.findall(r"\w+", title) if len(w) > 3}
        summary_words = {w.lower() for w in re.findall(r"\w+", promised_content_summary) if len(w) > 3}
        if title_words and not (title_words & summary_words):
            issues.append("título parece não corresponder ao conteúdo prometido")
            score -= 0.25

        return TitleAssessment(score=max(0.0, score), issues=issues, misleading=misleading)


@dataclass
class ThumbnailAssessment:
    score: float
    issues: list[str] = field(default_factory=list)


class ThumbnailEngine:
    def evaluate(self, *, matches_content: bool, has_clear_focal_point: bool, text_legible_at_small_size: bool) -> ThumbnailAssessment:
        issues: list[str] = []
        score = 1.0
        if not matches_content:
            issues.append("thumbnail não corresponde ao conteúdo real")
            score -= 0.5
        if not has_clear_focal_point:
            issues.append("sem ponto focal claro")
            score -= 0.2
        if not text_legible_at_small_size:
            issues.append("texto ilegível em miniatura")
            score -= 0.2
        return ThumbnailAssessment(score=max(0.0, score), issues=issues)


@dataclass
class SeoAssessment:
    score: float
    issues: list[str] = field(default_factory=list)


class SeoEngine:
    def evaluate(self, *, title: str, description: str, tags: list[str], search_intent_keywords: list[str]) -> SeoAssessment:
        issues: list[str] = []
        score = 1.0

        if not description or len(description) < 40:
            issues.append("descrição muito curta para SEO eficaz")
            score -= 0.2

        if not tags:
            issues.append("nenhuma tag/metadado informado")
            score -= 0.15

        content_blob = f"{title} {description} {' '.join(tags)}".lower()
        missing = [kw for kw in search_intent_keywords if kw.lower() not in content_blob]
        if missing:
            issues.append(f"palavras-chave de intenção de busca ausentes: {', '.join(missing)}")
            score -= 0.1 * len(missing)

        return SeoAssessment(score=max(0.0, score), issues=issues)
