"""
atlas.radar.worker
=====================
ATLAS+RADAR — microcorreção final: EXECUÇÃO AUTOMÁTICA REAL DO RADAR
NO RUNTIME.

Até aqui, `RadarScheduler` já persistia estado, controlava intervalo,
concorrência, `last_run`/`next_run` e executava corretamente — mas só
quando alguém chamava `run_if_due()` (via
`AtlasDirector.run_scheduled_radar_scan()`) manualmente. Não havia
nenhum processo de fundo que fizesse essa chamada sozinho.

`RadarWorker` é esse processo de fundo: uma thread (stdlib puro, sem
dependência de serviço externo pago) que periodicamente chama
`RadarScheduler.run_if_due()` — a MESMA operação que já existia,
já testada, já atômica/idempotente/concorrente-segura. O worker não
reimplementa NENHUMA lógica de agendamento — só decide QUANDO perguntar
"está devido?", em intervalos curtos e configuráveis, usando
`threading.Event.wait()` (nunca `time.sleep()` em loop apertado) para
não consumir CPU à toa e para poder ser interrompido instantaneamente
no shutdown.

Garantias:
- Nunca busy loop: `Event.wait(timeout=...)` bloqueia a thread sem
  consumir CPU entre verificações.
- Erro não mata o worker: qualquer exceção de `run_if_due()` (já
  logada como FAILED pelo próprio `RadarScheduler`) é capturada aqui —
  o loop continua rodando no próximo ciclo.
- Backoff em falhas repetidas: cada falha CONSECUTIVA dobra o tempo de
  espera até `max_backoff_seconds`; um sucesso zera o contador.
- start()/stop() idempotentes.
- Nunca ação crítica: o worker só conhece `RadarScheduler`, que só
  conhece `CommercialOpportunityRadar.scan()` (leitura/descoberta) —
  nenhuma referência a `EscalationDesk` existe neste módulo.
"""

from __future__ import annotations

import threading

from atlas.radar.scheduler import RadarScheduler


class WorkerShutdownTimeout(Exception):
    """MICROCORREÇÃO (shutdown residual — falha real reproduzida por
    auditoria independente): levantada por `RadarWorker.stop()` quando
    `thread.join(timeout)` esgota o prazo e a thread AINDA está viva.
    Nunca finge sucesso — o chamador precisa saber explicitamente que
    o worker não parou a tempo, nunca inferir isso indiretamente de um
    `is_running()` que mentiria `False` para uma thread ainda viva."""


class RadarWorker:
    def __init__(
        self,
        *,
        scheduler: RadarScheduler,
        poll_interval_seconds: float = 1.0,
        niche: str | None = None,
        max_backoff_seconds: float = 300.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds precisa ser positivo")
        self.scheduler = scheduler
        self.poll_interval_seconds = poll_interval_seconds
        self.niche = niche
        self.max_backoff_seconds = max_backoff_seconds

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._poll_count = 0

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Idempotente: se já estiver rodando, não faz nada (nunca cria
        uma segunda thread concorrente do mesmo worker)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._consecutive_failures = 0
            self._thread = threading.Thread(target=self._run_loop, name="RadarWorker", daemon=True)
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        """Sinaliza parada e aguarda a thread encerrar — nunca deixa
        thread órfã. Idempotente: chamar sem thread ativa é um no-op.

        MICROCORREÇÃO (shutdown residual — fail-closed): a referência
        da thread SÓ é descartada (`self._thread = None`) quando ela
        REALMENTE terminou (`thread.is_alive()` é `False` após o
        `join`). Antes, `self._thread = None` era atribuído
        incondicionalmente após o `join`, mesmo quando o timeout se
        esgotava com a thread ainda viva — isso fazia `is_running()`
        mentir `False` para uma thread genuinamente ainda em execução
        (falso sucesso de shutdown). Agora, se o timeout se esgota com
        a thread ainda viva, a referência é MANTIDA (nunca descartada)
        e `WorkerShutdownTimeout` é levantada explicitamente — o
        chamador nunca precisa inferir a falha indiretamente."""
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join(timeout=timeout)
        with self._lock:
            if thread.is_alive():
                # CORREÇÃO CRÍTICA: nunca descarta a referência de uma
                # thread viva — `self._thread` permanece exatamente
                # como estava, para que `is_running()` continue
                # reportando `True` corretamente.
                raise WorkerShutdownTimeout(
                    f"RadarWorker não encerrou dentro de {timeout}s — thread ainda viva, referência preservada"
                )
            if self._thread is thread:
                self._thread = None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def poll_count(self) -> int:
        """Quantas vezes o loop verificou se havia um scan devido —
        útil em testes para confirmar execução automática repetida."""
        return self._poll_count

    # ------------------------------------------------------------------
    # Loop interno
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            sleep_for = self.poll_interval_seconds
            try:
                self.scheduler.run_if_due(niche=self.niche)
                self._consecutive_failures = 0
                self._last_error = None
            except Exception as exc:  # nunca deixa uma falha matar o worker
                self._consecutive_failures += 1
                self._last_error = str(exc)
                backoff = self.poll_interval_seconds * (2 ** self._consecutive_failures)
                sleep_for = min(backoff, self.max_backoff_seconds)
            finally:
                self._poll_count += 1

            # espera interrompível — NUNCA time.sleep() em loop apertado;
            # `stop()` (que seta o Event) acorda esta espera na hora,
            # permitindo shutdown limpo mesmo em pleno backoff longo.
            self._stop_event.wait(timeout=sleep_for)
