"""
core.learning.loop
======================
BLOCO FINAL 1 — item 6 (LEARNING LOOP): registra resultados REAIS,
compara previsto × realizado, aprende com acertos e erros, propaga
aprendizados VALIDADOS entre agentes sem contaminar outros canais/
projetos, persiste após restart.

Princípios (mesma disciplina de toda a base — zero dados fabricados):
- Uma `Prediction` só é comparável depois que uma `Outcome` REAL for
  registrada — nunca se "inventa" o resultado.
- `LearningRecord` é sempre escopado (channel_id/project_id) — nunca
  vaza para outro escopo silenciosamente.
- Uma `ValidatedLearning` só é promovida (fica visível para OUTROS
  agentes/escopos) depois de atingir uma barra estatística mínima
  (tamanho de amostra + consistência de direção) — nunca a partir de
  uma única observação.
- Propagação cross-escopo (`applies_globally=True`) é uma decisão
  EXPLÍCITA de quem valida (nunca automática) — mantém o requisito de
  "propagar aprendizados validados... sem contaminar outros canais/
  projetos".
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class PredictionStatus(str, Enum):
    PENDING = "PENDING"          # previsão registrada, ainda sem outcome real
    COMPARED = "COMPARED"        # outcome real registrado e comparado
    EXPIRED = "EXPIRED"          # nunca recebeu outcome dentro da janela esperada


@dataclass
class Prediction:
    prediction_id: str
    agent_id: str
    metric_name: str
    predicted_value: float
    scope_channel_id: str | None
    scope_project_id: str | None
    context: str
    created_at: float = field(default_factory=time.time)
    status: PredictionStatus = PredictionStatus.PENDING
    evidence_ids: list = field(default_factory=list)
    # COMANDO 2/6: valor REAL observado ANTES da ação (linha de base).
    # Com ela, "acertou a direção" significa "previu alta/baixa em
    # relação à base e a realidade foi no mesmo sentido". Opcional e
    # aditivo — previsões antigas ficam None (critério legado + calibração).
    baseline_value: float | None = None

    @staticmethod
    def new(
        *, agent_id: str, metric_name: str, predicted_value: float, context: str,
        scope_channel_id: str | None = None, scope_project_id: str | None = None,
        evidence_ids: list | None = None, baseline_value: float | None = None,
    ) -> "Prediction":
        return Prediction(
            prediction_id=f"pred_{uuid.uuid4().hex[:12]}", agent_id=agent_id, metric_name=metric_name,
            predicted_value=predicted_value, scope_channel_id=scope_channel_id,
            scope_project_id=scope_project_id, context=context, evidence_ids=list(evidence_ids or []),
            baseline_value=baseline_value,
        )


@dataclass
class LearningRecord:
    """Previsto × realizado — a unidade atômica do aprendizado. Nunca
    fabricado: `actual_value` sempre veio de uma observação real
    registrada pelo chamador (nunca calculado/estimado aqui)."""

    record_id: str
    prediction_id: str
    agent_id: str
    metric_name: str
    predicted_value: float
    actual_value: float
    scope_channel_id: str | None
    scope_project_id: str | None
    compared_at: float = field(default_factory=time.time)
    baseline_value: float | None = None

    @property
    def error(self) -> float:
        return self.actual_value - self.predicted_value

    @property
    def absolute_percentage_error(self) -> float | None:
        if self.actual_value == 0:
            return None  # divisão por zero -- nunca inventa um número aqui
        return abs(self.error) / abs(self.actual_value)

    @property
    def direction_correct(self) -> bool:
        """Acertou a DIREÇÃO (previu alta e subiu, ou previu baixa e
        desceu) — mais robusto que erro absoluto para aprendizado
        qualitativo (ex: "esse gancho converte melhor").

        CORREÇÃO (Comando 2/6 — defeito provado): o critério antigo
        comparava só o SINAL de previsto e realizado. Para métricas de
        NÍVEL não-negativas (conversão, receita, CTR, vendas) isso é
        tautológico — previsto 10% × real 1% (erro de 900%) contava como
        acerto, e 5 refutações seguidas eram PROMOVIDAS a aprendizado
        VALIDATED com `direction_accuracy=1.0`.
        - Com `baseline_value`: direção relativa à linha de base real
          (sinal de previsto−base == sinal de real−base).
        - Sem linha de base (legado): sinal E calibração — o erro
          percentual não pode exceder `_MAX_LEGACY_CALIBRATION_ERROR`
          (um erro maior que metade do valor realizado nunca confirma
          uma previsão)."""
        if self.baseline_value is not None:
            predicted_delta = self.predicted_value - self.baseline_value
            actual_delta = self.actual_value - self.baseline_value
            if predicted_delta == 0:
                return actual_delta == 0
            return (predicted_delta > 0) == (actual_delta > 0) and actual_delta != 0
        if self.predicted_value != 0 and (self.predicted_value >= 0) != (self.actual_value >= 0):
            return False
        ape = self.absolute_percentage_error
        if ape is None:
            return self.predicted_value == 0
        return ape <= _MAX_LEGACY_CALIBRATION_ERROR


class ValidationStatus(str, Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    VALIDATED = "VALIDATED"
    CONTRADICTED = "CONTRADICTED"  # amostra suficiente, mas SEM consistência -- nunca promovido


@dataclass
class ValidatedLearning:
    learning_id: str
    metric_name: str
    scope_channel_id: str | None
    scope_project_id: str | None
    sample_size: int
    direction_accuracy: float
    mean_absolute_percentage_error: float | None
    statement: str
    applies_globally: bool = False
    validated_by: str | None = None
    validated_at: float = field(default_factory=time.time)


# CORREÇÃO (item 6 — barra estatística mínima, nunca promove a partir
# de amostra pequena/inconsistente): configurável, mas com padrão
# conservador.
_MIN_SAMPLE_SIZE = 5
_MIN_DIRECTION_ACCURACY = 0.7
# Previsões SEM linha de base: erro percentual máximo para contar como
# acerto (ver `LearningRecord.direction_correct`).
_MAX_LEGACY_CALIBRATION_ERROR = 0.5


class LearningLoop:
    """Registro central do Learning Loop — em memória por padrão;
    quando `db_path` é fornecido, persiste predictions/outcomes/
    validated learnings em SQLite (sobrevive a restart, mesmo padrão
    de todo o resto do sistema)."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = db_path
        self._predictions: dict = {}
        self._records: list = []
        self._validated: dict = {}  # (metric_name, scope_channel_id, scope_project_id) -> ValidatedLearning
        if db_path:
            self._load_from_db()

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------

    def record_prediction(self, prediction: Prediction) -> Prediction:
        self._predictions[prediction.prediction_id] = prediction
        self._persist_prediction(prediction)
        return prediction

    def record_outcome(self, *, prediction_id: str, actual_value: float) -> LearningRecord:
        """Compara a previsão registrada contra um resultado REAL —
        nunca aceita um `actual_value` sem uma `Prediction` prévia
        genuína (evita "aprender" de comparações fabricadas)."""
        prediction = self._predictions.get(prediction_id)
        if prediction is None:
            raise KeyError(f"prediction_id '{prediction_id}' não encontrada — outcome sem previsão correspondente")
        record = LearningRecord(
            record_id=f"lrec_{uuid.uuid4().hex[:12]}", prediction_id=prediction_id, agent_id=prediction.agent_id,
            metric_name=prediction.metric_name, predicted_value=prediction.predicted_value,
            actual_value=actual_value, scope_channel_id=prediction.scope_channel_id,
            scope_project_id=prediction.scope_project_id, baseline_value=prediction.baseline_value,
        )
        prediction.status = PredictionStatus.COMPARED
        self._records.append(record)
        self._persist_record(record)
        self._persist_prediction(prediction)
        return record

    # ------------------------------------------------------------------
    # Validação e propagação
    # ------------------------------------------------------------------

    def evaluate_learning(
        self, *, metric_name: str, scope_channel_id: str | None = None, scope_project_id: str | None = None,
    ) -> ValidationStatus:
        """Avalia se os registros ACUMULADOS para este escopo+métrica
        já atingem a barra estatística mínima. NUNCA promove a partir
        de uma amostra pequena; NUNCA promove se a direção não for
        consistente o suficiente."""
        matching = [
            r for r in self._records
            if r.metric_name == metric_name and r.scope_channel_id == scope_channel_id
            and r.scope_project_id == scope_project_id
        ]
        if len(matching) < _MIN_SAMPLE_SIZE:
            return ValidationStatus.INSUFFICIENT_DATA
        accuracy = sum(1 for r in matching if r.direction_correct) / len(matching)
        if accuracy < _MIN_DIRECTION_ACCURACY:
            return ValidationStatus.CONTRADICTED
        return ValidationStatus.VALIDATED

    def promote_validated_learning(
        self, *, metric_name: str, statement: str, validated_by: str,
        scope_channel_id: str | None = None, scope_project_id: str | None = None, applies_globally: bool = False,
    ) -> ValidatedLearning:
        """CORREÇÃO (item 6 — propagação sem contaminação): só promove
        se `evaluate_learning()` retornar `VALIDATED` — nunca a partir
        de uma alegação isolada. `applies_globally` é uma decisão
        EXPLÍCITA de `validated_by` (nunca automática) — sem isso, o
        aprendizado só é visível dentro do MESMO escopo
        (channel_id/project_id) em que foi observado."""
        status = self.evaluate_learning(
            metric_name=metric_name, scope_channel_id=scope_channel_id, scope_project_id=scope_project_id,
        )
        if status != ValidationStatus.VALIDATED:
            raise ValueError(
                f"aprendizado para '{metric_name}' (escopo channel={scope_channel_id}/project={scope_project_id}) "
                f"não atingiu validação estatística ({status.value}) — não pode ser promovido"
            )
        matching = [
            r for r in self._records
            if r.metric_name == metric_name and r.scope_channel_id == scope_channel_id
            and r.scope_project_id == scope_project_id
        ]
        accuracy = sum(1 for r in matching if r.direction_correct) / len(matching)
        apes = [r.absolute_percentage_error for r in matching if r.absolute_percentage_error is not None]
        mape = sum(apes) / len(apes) if apes else None

        learning = ValidatedLearning(
            learning_id=f"vlearn_{uuid.uuid4().hex[:12]}", metric_name=metric_name,
            scope_channel_id=scope_channel_id, scope_project_id=scope_project_id, sample_size=len(matching),
            direction_accuracy=accuracy, mean_absolute_percentage_error=mape, statement=statement,
            applies_globally=applies_globally, validated_by=validated_by,
        )
        key = (metric_name, scope_channel_id, scope_project_id)
        self._validated[key] = learning
        self._persist_validated(learning)
        return learning

    def learnings_visible_to(
        self, *, scope_channel_id: str | None = None, scope_project_id: str | None = None,
    ) -> list:
        """CORREÇÃO (item 6 — nunca contamina outros canais/projetos):
        devolve os aprendizados validados que se aplicam a ESTE
        escopo — os do MESMO escopo exato, MAIS quaisquer marcados
        explicitamente `applies_globally=True` (decisão humana/ATLAS
        prévia, nunca automática)."""
        results = []
        for learning in self._validated.values():
            same_scope = (
                learning.scope_channel_id == scope_channel_id and learning.scope_project_id == scope_project_id
            )
            if same_scope or learning.applies_globally:
                results.append(learning)
        return results

    # ------------------------------------------------------------------
    # Persistência (mesmo padrão de todo o sistema — WAL, close garantido)
    # ------------------------------------------------------------------

    def _persist_prediction(self, prediction: Prediction) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO learning_predictions "
                "(prediction_id, agent_id, metric_name, predicted_value, scope_channel_id, scope_project_id, "
                "context, created_at, status, evidence_ids, baseline_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prediction.prediction_id, prediction.agent_id, prediction.metric_name,
                    prediction.predicted_value, prediction.scope_channel_id, prediction.scope_project_id,
                    prediction.context, prediction.created_at, prediction.status.value,
                    json.dumps(prediction.evidence_ids), prediction.baseline_value,
                ),
            )

    def _persist_record(self, record: LearningRecord) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO learning_records "
                "(record_id, prediction_id, agent_id, metric_name, predicted_value, actual_value, "
                "scope_channel_id, scope_project_id, compared_at, baseline_value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.record_id, record.prediction_id, record.agent_id, record.metric_name,
                    record.predicted_value, record.actual_value, record.scope_channel_id,
                    record.scope_project_id, record.compared_at, record.baseline_value,
                ),
            )

    def _persist_validated(self, learning: ValidatedLearning) -> None:
        if not self.db_path:
            return
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO learning_validated "
                "(learning_id, metric_name, scope_channel_id, scope_project_id, sample_size, "
                "direction_accuracy, mean_absolute_percentage_error, statement, applies_globally, "
                "validated_by, validated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    learning.learning_id, learning.metric_name, learning.scope_channel_id,
                    learning.scope_project_id, learning.sample_size, learning.direction_accuracy,
                    learning.mean_absolute_percentage_error, learning.statement,
                    int(learning.applies_globally), learning.validated_by, learning.validated_at,
                ),
            )

    def _load_from_db(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            for row in conn.execute("SELECT * FROM learning_predictions").fetchall():
                self._predictions[row["prediction_id"]] = Prediction(
                    prediction_id=row["prediction_id"], agent_id=row["agent_id"], metric_name=row["metric_name"],
                    predicted_value=row["predicted_value"], scope_channel_id=row["scope_channel_id"],
                    scope_project_id=row["scope_project_id"], context=row["context"], created_at=row["created_at"],
                    status=PredictionStatus(row["status"]), evidence_ids=json.loads(row["evidence_ids"]),
                    baseline_value=row["baseline_value"],
                )
            for row in conn.execute("SELECT * FROM learning_records").fetchall():
                self._records.append(LearningRecord(
                    record_id=row["record_id"], prediction_id=row["prediction_id"], agent_id=row["agent_id"],
                    metric_name=row["metric_name"], predicted_value=row["predicted_value"],
                    actual_value=row["actual_value"], scope_channel_id=row["scope_channel_id"],
                    scope_project_id=row["scope_project_id"], compared_at=row["compared_at"],
                    baseline_value=row["baseline_value"],
                ))
            for row in conn.execute("SELECT * FROM learning_validated").fetchall():
                key = (row["metric_name"], row["scope_channel_id"], row["scope_project_id"])
                self._validated[key] = ValidatedLearning(
                    learning_id=row["learning_id"], metric_name=row["metric_name"],
                    scope_channel_id=row["scope_channel_id"], scope_project_id=row["scope_project_id"],
                    sample_size=row["sample_size"], direction_accuracy=row["direction_accuracy"],
                    mean_absolute_percentage_error=row["mean_absolute_percentage_error"],
                    statement=row["statement"], applies_globally=bool(row["applies_globally"]),
                    validated_by=row["validated_by"], validated_at=row["validated_at"],
                )

    def all_records(self) -> list:
        return list(self._records)

    def all_predictions(self) -> list:
        return list(self._predictions.values())
