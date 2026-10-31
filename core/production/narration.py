"""
core.production.narration
===========================
NARRATION_TEXT deve conter SOMENTE texto falado (Seção 10). Nunca
enviar para voz: [SCENE], [CUT], [IMAGE], timestamps, tags, prompts,
instruções técnicas, metadados.

Separa estruturalmente: NARRATION_TEXT, SCENE_INSTRUCTIONS,
VISUAL_PROMPT, EDITING_INSTRUCTIONS, MUSIC_INSTRUCTIONS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Qualquer coisa entre colchetes é considerada instrução técnica, não fala.
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
# Timestamps tipo 00:00:12 ou 00:12
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b")
# Linhas que começam com rótulos técnicos conhecidos
_TECH_LABEL_RE = re.compile(
    r"^\s*(SCENE|CUT|IMAGE|VISUAL|EDIT|MUSIC|SFX|CAMERA|TRANSITION)\s*:",
    re.IGNORECASE,
)


@dataclass
class StructuredScript:
    narration_text: str
    scene_instructions: list[str] = field(default_factory=list)
    visual_prompts: list[str] = field(default_factory=list)
    editing_instructions: list[str] = field(default_factory=list)
    music_instructions: list[str] = field(default_factory=list)


class NarrationLeakDetected(Exception):
    """Levantado quando um texto que iria para a engine de voz ainda
    contém marcações técnicas — bloqueia antes de sintetizar áudio."""


def split_raw_script(raw_script: str) -> StructuredScript:
    """Recebe um roteiro bruto com marcações mistas e separa em blocos
    estruturados. Formato esperado de entrada (por linha):

        NARRATION: texto falado aqui
        SCENE: descrição da cena
        VISUAL: prompt de geração visual
        EDIT: instrução de edição
        MUSIC: instrução de trilha

    Linhas sem rótulo são tratadas como narração por padrão, mas
    colchetes/timestamps dentro delas são removidos antes de seguir
    para a voz.
    """
    narration_lines: list[str] = []
    scenes: list[str] = []
    visuals: list[str] = []
    edits: list[str] = []
    music: list[str] = []

    for raw_line in raw_script.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = re.match(r"^(NARRATION|SCENE|VISUAL|EDIT|MUSIC)\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            label, content = match.group(1).upper(), match.group(2).strip()
            if label == "NARRATION":
                narration_lines.append(_strip_technical_markup(content))
            elif label == "SCENE":
                scenes.append(content)
            elif label == "VISUAL":
                visuals.append(content)
            elif label == "EDIT":
                edits.append(content)
            elif label == "MUSIC":
                music.append(content)
            continue

        # linha sem rótulo: assume narração, mas ainda sanitiza
        narration_lines.append(_strip_technical_markup(line))

    return StructuredScript(
        narration_text=" ".join(part for part in narration_lines if part),
        scene_instructions=scenes,
        visual_prompts=visuals,
        editing_instructions=edits,
        music_instructions=music,
    )


def _strip_technical_markup(text: str) -> str:
    text = _BRACKET_TAG_RE.sub("", text)
    text = _TIMESTAMP_RE.sub("", text)
    text = _TECH_LABEL_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def assert_clean_for_voice(narration_text: str) -> None:
    """Guarda final antes de mandar para a Voice Engine. Bloqueia se
    ainda restar qualquer marcação técnica."""
    if _BRACKET_TAG_RE.search(narration_text):
        raise NarrationLeakDetected("NARRATION_TEXT contém marcações [ ] não removidas")
    if _TECH_LABEL_RE.search(narration_text):
        raise NarrationLeakDetected("NARRATION_TEXT contém rótulo técnico não removido")
