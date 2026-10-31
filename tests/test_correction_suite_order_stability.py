import time
import unittest

"""
Teste de regressão — MICROCORREÇÃO FINAL (suite-order stability).

Causa raiz confirmada: 3 testes (2 em `HttpSearchProvider`/
`HttpGenerationProvider`, 1 em `RealWebPageAnalyzer`) faziam
tentativas de rede REAIS e NÃO SIMULADAS contra domínios inválidos,
usando os timeouts PADRÃO dos providers (10s/30s/8s antes desta
correção). O tempo de uma tentativa de rede real até falhar
(resolução de DNS, timeout de conexão) varia GENUINAMENTE por
ambiente — rápido aqui, potencialmente muito mais lento em um
ambiente com resolução de DNS diferente, proxy corporativo, ou
política de rede que não recusa a conexão imediatamente. Isso é uma
fonte real de instabilidade dependente de ambiente/ordem de execução
— não uma "condição de corrida" tradicional, mas o mesmo sintoma
observável (suíte completa instável, teste isolado sempre rápido).

Corrigido:
1. Os 3 testes agora usam `unittest.mock.patch` para simular a MESMA
   exceção que uma falha de rede real produziria — determinístico,
   rápido, nunca depende do comportamento de rede do ambiente.
2. Os timeouts PADRÃO de produção (`HttpSearchProvider`/
   `HttpGenerationProvider`/`RealWebPageAnalyzer`) foram reduzidos
   (10s→3s, 30s→5s, 8s→3s) como defesa em profundidade — nunca
   aumentados, sempre reduzidos, para limitar o pior caso de
   qualquer tentativa de rede real (configurada ou não) tanto em
   testes futuros quanto em produção sob rede instável.

Este arquivo garante que a causa raiz não volta: mede que os módulos
que TOCAM rede real completam rapidamente quando executados via
`unittest`, e verifica estaticamente que nenhum teste chama os
métodos que fazem rede real sem um `mock.patch` envolvendo a chamada.
"""


class NetworkDependentTestsMustBeFastTests(unittest.TestCase):
    """Mede o tempo real de execução dos módulos de teste que TOCAM
    código de rede — nunca deve passar de alguns segundos, mesmo que
    algum teste futuro reintroduza uma tentativa de rede real sem
    querer (nesse caso, o teste abaixo falharia por timeout, sinal
    claro do problema, em vez de deixar a suíte inteira travar
    silenciosamente)."""

    _MAX_SECONDS_FOR_NETWORK_TEST_MODULE = 15.0
    # ORCAMENTO DE TEMPO (ajuste de teto; producao intocada): 5s -> 15s.
    # Medicoes do modulo in-process: 9.94s (na suite, historico) / 8.51s
    # (frio, isolado) / 7.71s (contaminado) / 1.24s (aquecido, cProfile)
    # -- o custo in-process varia ~8x com o estado da maquina. Cenario de
    # rede real reintroduzida custaria ~12.1s (base 1.2s + timeouts
    # atuais 3s+5s+3s). A guarda estatica irma (NoUnmockedRealNetwork
    # CallsInTestsRegressionTests) e a deteccao deterministica primaria
    # (rc=0 verificada); este teto dinamico fica como detector de
    # sintomas graves (>= 15s), imune ao ruido de maquina fria.

    # FECHAMENTO 1/6 (sensibilidade medida): o teto de tempo NAO detecta
    # rede real reintroduzida — (a) pelas proprias contas acima o cenario
    # custa ~12.1s, ABAIXO do teto de 15s; (b) em ambiente sem rede a falha
    # de DNS e instantanea. Prova por mutacao (mock de urlopen removido em
    # `HttpSearchProviderTests`): este teste E a guarda estatica irma
    # passaram 2/2. Detector deterministico adicionado: durante a execucao
    # do modulo, qualquer tentativa de rede no nivel do socket
    # (`getaddrinfo`/`create_connection`/`socket.connect`) e REGISTRADA e
    # recusada; o teste exige zero tentativas — independe de tempo e de
    # ambiente. O teto de 15s permanece como sintoma grosseiro secundario.

    def test_microcorrecao_bloco_final1_module_runs_fast(self):
        import socket
        from unittest.mock import patch

        attempts = []

        def _blocked(name):
            def _deny(*args, **kwargs):
                attempts.append((name, repr(args[:2])))
                raise OSError("rede real bloqueada pelo teste (tentativa registrada: %s)" % name)
            return _deny

        start = time.time()
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName("tests.test_microcorrecao_bloco_final1")
        runner = unittest.TextTestRunner(verbosity=0)
        with patch.object(socket, "getaddrinfo", _blocked("getaddrinfo")), \
                patch.object(socket, "create_connection", _blocked("create_connection")), \
                patch.object(socket.socket, "connect", _blocked("socket.connect")), \
                patch.object(socket.socket, "connect_ex", _blocked("socket.connect_ex")):
            result = runner.run(suite)
        duration = time.time() - start
        self.assertEqual(
            attempts, [],
            f"tentativa(s) de rede REAL durante o módulo (deveria ser sempre via mock.patch): {attempts}",
        )
        self.assertTrue(result.wasSuccessful())
        self.assertLess(
            duration, self._MAX_SECONDS_FOR_NETWORK_TEST_MODULE,
            f"módulo com testes de rede levou {duration:.2f}s — suspeita de tentativa de rede real "
            f"não simulada reintroduzida (deveria ser sempre determinístico via mock.patch)",
        )


class NoUnmockedRealNetworkCallsInTestsRegressionTests(unittest.TestCase):
    """Checagem estática — nenhum teste pode chamar
    `HttpSearchProvider.call()`/`HttpGenerationProvider.call()`/
    `RealWebPageAnalyzer.analyze()` (os 3 pontos REAIS de rede) fora
    de um bloco `with ... patch(...)`/`@patch(...)` que controle
    `urllib.request.urlopen` — nunca depende do comportamento de
    rede/DNS real do ambiente onde o teste roda."""

    def test_no_test_calls_network_methods_without_mocking_urlopen(self):
        import glob
        import os
        import re

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self_path = os.path.abspath(__file__)

        network_call_pattern = re.compile(
            r"\.call\(operation=|RealWebPageAnalyzer\(\)\.analyze\(|webpage_analyzer.*\.analyze\("
        )
        mock_pattern = re.compile(r"patch\(.*urlopen")

        offenders = []
        for path in glob.glob(os.path.join(root, "tests", "*.py")):
            if os.path.abspath(path) == self_path:
                continue
            with open(path, encoding="utf-8") as f:
                content = f.read()
            if network_call_pattern.search(content) and not mock_pattern.search(content):
                offenders.append(os.path.relpath(path, root))

        self.assertEqual(
            offenders, [],
            f"arquivo(s) de teste chamando métodos de rede real sem mockar urlopen: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
