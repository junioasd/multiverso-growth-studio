"""
Helper de teste (FECHAMENTO 1/6): força a REUTILIZAÇÃO REAL de um PID.

Uso: pid_reuse_occupier.py <pid_alvo> <segundos_max>
     pid_reuse_occupier.py --advance <pid_minimo> <segundos_max>

Cicla o espaço de PIDs com fork()+_exit() até o SO entregar o PID-alvo a
um processo NOVO. Esse processo (outro processo, outra identidade
temporal) imprime "HELD <pid>" e permanece vivo até o stdin fechar. Se o
PID não voltar dentro do prazo, imprime "MISS" e sai com código 3 (o teste
então é pulado, nunca aprovado).

Roda em processo separado e single-thread — fork() dentro do processo da
suíte (com threads de heartbeat) seria arriscado.
"""
import os
import sys
import time


def advance(min_pid: int, seconds: float) -> int:
    """Modo `--advance`: cicla até o PRÓXIMO PID alocado ser >= min_pid.
    No kernel Linux, após o wrap de `pid_max` a alocação recomeça em 300
    (PIDs < 300 são reservados e NUNCA voltam pelo ciclo) — um detentor
    com PID baixo tornaria a reutilização real impossível."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        if pid >= min_pid:
            return 0
    return 3


def main() -> int:
    if sys.argv[1] == "--advance":
        return advance(int(sys.argv[2]), float(sys.argv[3]))
    target = int(sys.argv[1])
    deadline = time.monotonic() + float(sys.argv[2])
    while time.monotonic() < deadline:
        pid = os.fork()
        if pid == 0:
            if os.getpid() == target:
                sys.stdout.write("HELD %d\n" % os.getpid())
                sys.stdout.flush()
                sys.stdin.read()  # vivo até o teste fechar o stdin
            os._exit(0)
        if pid == target:
            return 0  # o filho que detém o PID continua vivo sozinho
        os.waitpid(pid, 0)
    sys.stdout.write("MISS\n")
    sys.stdout.flush()
    return 3


if __name__ == "__main__":
    sys.exit(main())
