"""
youtube_strategy.alignment_checker
=====================================
YouTube AI Alignment (Seção 23). Checklist obrigatório antes de
publicar.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AlignmentCheckInput:
    audience_appropriate: bool | None = None
    thumbnail_matches_content: bool | None = None
    title_matches_promise: bool | None = None
    strong_opening_seconds: bool | None = None
    maintains_interest: bool | None = None
    has_original_value: bool | None = None
    excessive_repetition: bool | None = None          # True = problema
    reused_without_transformation: bool | None = None  # True = problema
    feels_industrial_repetitive: bool | None = None    # True = problema
    fulfills_search_intent: bool | None = None
    coherent_channel_content_audience: bool | None = None


@dataclass
class AlignmentReport:
    ready_to_publish: bool
    unresolved_questions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


_POSITIVE_QUESTIONS = {
    "audience_appropriate": "O conteúdo é adequado ao público correto?",
    "thumbnail_matches_content": "A thumbnail corresponde ao conteúdo?",
    "title_matches_promise": "O título corresponde à promessa?",
    "strong_opening_seconds": "Os primeiros segundos são fortes?",
    "maintains_interest": "O vídeo mantém interesse?",
    "has_original_value": "Há valor original?",
    "fulfills_search_intent": "O vídeo cumpre a intenção de busca?",
    "coherent_channel_content_audience": "Há coerência entre canal, conteúdo e audiência?",
}

_NEGATIVE_QUESTIONS = {
    "excessive_repetition": "Há repetição excessiva?",
    "reused_without_transformation": "Há conteúdo reutilizado sem transformação suficiente?",
    "feels_industrial_repetitive": "Parece produção industrial repetitiva?",
}


class YouTubeAlignmentChecker:
    def check(self, data: AlignmentCheckInput) -> AlignmentReport:
        unresolved: list[str] = []
        problems: list[str] = []
        values = data.__dict__

        for field_name, question in _POSITIVE_QUESTIONS.items():
            value = values[field_name]
            if value is None:
                unresolved.append(question)
            elif value is False:
                problems.append(question)

        for field_name, question in _NEGATIVE_QUESTIONS.items():
            value = values[field_name]
            if value is None:
                unresolved.append(question)
            elif value is True:
                problems.append(question)

        ready = len(unresolved) == 0 and len(problems) == 0
        return AlignmentReport(ready_to_publish=ready, unresolved_questions=unresolved, problems=problems)
