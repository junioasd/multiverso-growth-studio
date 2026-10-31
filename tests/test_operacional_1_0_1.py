"""
v1.0.1-OPERATIONAL-CANDIDATE — kill switch, worker, scheduler, logging,
custo de IA/API, backup, configuração de produção, cookie e readiness.
Caminho real: `build_app`, SQLite, servidor HTTP LOCAL, e os comandos
`main.py backup|scheduler|worker|killswitch` executados como PROCESSOS reais.
Nenhuma integração externa real é tocada.
"""
# v1.0.10 (auditoria 1.0.9, B3): uso SEM reserva passou a exigir `usage_event_id` (identidade idempotente
# do evento). Expectativa antiga: registrar sem identidade — era o próprio defeito (retry duplicava custo).
# Cada chamada recebeu um id DISTINTO, preservando a intenção original (eventos distintos seguem distintos).
import http.server
import json
import logging
import os
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from api.bootstrap import build_app
from bridge.execution.navigator_real import ActionCriticality
from bridge.execution.platform_adapters import PlatformApiAdapter
from bridge.execution.vault import TokenType
from bridge.hub import ConnectorType, HealthStatus
from core.jobs.queue import JobQueue
from core.models import db as db_module
from core.operations import config_check, cost_guard, kill_switch, logging_setup
from core.operations.backup import run_backup, verify_backup
from core.operations.readiness import operational_readiness
from core.operations.scheduler import Scheduler
from core.operations.worker import run_worker_cycle
from core.security.auth import Principal, Role, principal_from_session
from navigator.planning import OperationPlanner, OperationRunner, StepSpec, schedule_advance
from tests._auth_test_helpers import fast_hash_password

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PWD = "TestPassword123"
BLOCK = kill_switch.BLOCK_CODE


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


class _Svc(http.server.BaseHTTPRequestHandler):
    hits = []

    def _ok(self):
        _Svc.hits.append(self.path)
        out = b'{"id": "x1", "status": "published"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._ok()

    def do_GET(self):
        self._ok()

    def log_message(self, *a):
        pass


class _Base(unittest.TestCase):
    def setUp(self):
        _Svc.hits = []
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "op.db")
        self.app = self._app(True)
        # como um processo operacional real (`main.py`) faz: configura o banco
        # global do kill switch e da guarda de custo; desfeito no tearDown.
        kill_switch.configure(self.db)
        cost_guard.configure(self.db)
        self.addCleanup(kill_switch.configure, None)
        self.addCleanup(cost_guard.configure, None)
        self.owner = principal_from_session(self.app.auth.current_session(self.app.auth.login("o@t.com", PWD)))
        self.atlas = self.app.atlas
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"

        class YT(PlatformApiAdapter):
            platform = "youtube"
            base_url = base

            def publish(self, **kw):
                return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)

            def status(self, **kw):
                return self._request(action="STATUS", method="GET", path="/status", params=kw)

        self.YT = YT
        self.ref = self.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN,
                                                      value="tok-local")
        self.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                             is_configured=True, authenticated=True, health=HealthStatus.OK,
                                             permissions_ok=True)
        self.atlas.navigator_real.register_adapter(platform="youtube", adapter=YT(vault=self.atlas.token_vault,
                                                                                  account_reference_id=self.ref))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)

    def _app(self, init):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password(PWD), db_path=self.db,
                         init_database=init)

    def on(self, reason="teste de emergência"):
        return kill_switch.activate(self.db, principal=self.owner, reason=reason, audit=self.app.facade.audit)

    def off(self, reason="fim do teste"):
        return kill_switch.deactivate(self.db, principal=self.owner, reason=reason, audit=self.app.facade.audit)

    def approved_publish(self, key="k1"):
        kw = dict(platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
                  params={"title": "t"}, actor="t", idempotency_key=key)
        esc = self.atlas.navigator_real.execute_action(**kw).escalation_id
        self.atlas.escalation_desk.approve(esc, decision="ok", resolved_by=self.owner)
        return kw, esc

    def runner(self):
        return OperationRunner(db_path=self.db, planner=OperationPlanner(bridge_hub=self.atlas.bridge),
                               navigator=self.atlas.navigator_real, escalation_desk=self.atlas.escalation_desk,
                               actor="T", observability=self.atlas.execution_observability)

    def cli(self, *args, env=None, stdin=None):
        e = dict(os.environ, DB_PATH=self.db, OWNER_EMAIL="o@t.com", OWNER_PASSWORD_HASH=fast_hash_password(PWD),
                 **(env or {}))
        return subprocess.run([sys.executable, "main.py", *args], cwd=ROOT, env=e, input=stdin, capture_output=True,
                              text=True, timeout=120)


# ------------------------------------------------------------ 1–9 kill switch
class KillSwitchTests(_Base):
    def test_01_blocks_navigator_real_even_with_approval(self):
        kw, esc = self.approved_publish()
        self.on()
        r = self.atlas.navigator_real.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
        self.assertEqual((r.success, r.delivery_state), (False, "NOT_SENT"))
        self.assertIn(BLOCK, r.reason)
        self.assertEqual(_Svc.hits, [])
        self.off()
        self.assertTrue(self.atlas.navigator_real.execute_action(**kw, adapter_method="publish",
                                                                 approved_escalation_id=esc).success)

    def test_01b_navigator_level_block_happens_before_dispatch_and_covers_browser(self):
        kw, esc = self.approved_publish(key="k-pre")
        self.on()
        r = self.atlas.navigator_real.execute_action(**kw, adapter_method="publish", approved_escalation_id=esc)
        self.assertIsNone(r.mechanism_used, "bloqueio ANTES de escolher mecanismo/despachar")
        visits = []

        class Browser:
            def navigate_and_extract(self, *, url):
                visits.append(url)
        from atlas.escalation import EscalationDesk
        from bridge.execution.navigator_real import NavigatorReal
        from bridge.hub import BridgeHub
        from core.security.audit import AuditTrail
        hub = BridgeHub()
        hub.register_connector(platform="site", connector_type=ConnectorType.BROWSER_AUTOMATION, is_configured=True,
                               authenticated=True, health=HealthStatus.OK, permissions_ok=True)
        nav = NavigatorReal(bridge_hub=hub, escalation_desk=EscalationDesk(AuditTrail()), browser_executor=Browser(),
                            db_path=self.db, audit=AuditTrail())
        rb = nav.execute_action(platform="site", action="GET_PAGE", criticality=ActionCriticality.ROUTINE, params={},
                                actor="t", url="https://example.invalid/p")
        self.assertIn(BLOCK, rb.reason)
        self.assertEqual(visits, [], "navegador não contorna o kill switch")

    def test_02_blocks_operation_runner_without_consuming_attempts(self):
        r = self.runner()
        run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(
            step_id="p", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": "t"},
            required_params=["title"])])
        r.advance(run)
        _, st, _ = r.load(run)
        self.atlas.escalation_desk.approve(st["p"]["escalation_id"], decision="ok", resolved_by=self.owner)
        self.on()
        r.advance(run)
        _, st, _ = r.load(run)
        self.assertNotEqual(st["p"]["status"], "RUNNING")
        self.assertEqual(st["p"]["attempts"], 0)
        self.assertIn(BLOCK, " ".join(st["p"]["reasons"]))
        self.assertEqual(_Svc.hits, [])
        self.off()
        self.assertEqual(r.advance(run), "SUCCEEDED")

    def test_03_09_worker_leaves_pending_job_untouched(self):
        r = self.runner()
        run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(
            step_id="p", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": "t"})])
        q = JobQueue(db_path=self.db)
        schedule_advance(q, run, tick=1)
        self.on()
        out = run_worker_cycle(db_path=self.db, runner=r, director=self.atlas, audit=self.app.facade.audit)
        self.assertEqual((out["processed"], out["blocked"]), ([], BLOCK))
        job = [j for j in q.all() if j.job_type == "navigator_run_advance"][0]
        self.assertEqual((job.state.value, job.attempts), ("QUEUED", 0), "job pendente não inicia")
        self.assertEqual(_Svc.hits, [])

    def test_04_scheduler_skips_external_tasks_but_recovers(self):
        self.on()
        res = {x["task"]: x["status"] for x in Scheduler(self.db).tick(now=1_000_000.0)}
        self.assertEqual(res, {"recover_stuck_jobs": "SUCCESS", "operation_runs": "SKIPPED_KILL_SWITCH"})

    def test_05_persists_across_restart_and_processes(self):
        self.on("persistência")
        out = self.cli("killswitch", "status")
        self.assertEqual(json.loads(out.stdout.strip().splitlines()[-1])["state"], "ENABLED", out.stderr[-300:])
        app2 = self._app(False)
        self.assertFalse(kill_switch.external_execution_allowed(app2.atlas.db_path))

    def test_06_07_only_owner_with_reason_and_audited(self):
        for role in (Role.ATLAS, Role.NAVIGATOR, Role.AGENT, Role.OPERATOR, Role.VIEWER):
            with self.assertRaises(PermissionError):
                kill_switch.activate(self.db, principal=Principal(subject="x", role=role, scopes={"*"}), reason="r")
        with self.assertRaises(ValueError):
            kill_switch.activate(self.db, principal=self.owner, reason="  ")
        self.on("incidente A")
        self.off("incidente A resolvido")
        h = kill_switch.history(self.db)
        # v1.0.2 (auditoria de readiness, item 8): o banco nasce com estado
        # inicial EXPLÍCITO registrado; depois vêm as mudanças do Owner.
        self.assertEqual([(e["state"], e["actor"]) for e in h], [("DISABLED", "system:init_db"),
                                                                 ("ENABLED", self.owner.subject),
                                                                 ("DISABLED", self.owner.subject)])
        self.assertTrue(all(e["reason"] and e["changed_at"] for e in h))
        actions = [e.action for e in self.app.facade.audit.all()]
        self.assertIn("kill_switch_enabled", actions)
        self.assertIn("kill_switch_disabled", actions)

    def test_08_retry_fallback_direct_api_webhook_and_paid_provider_cannot_bypass(self):
        kw, esc = self.approved_publish()
        self.on()
        for _ in range(3):
            self.assertFalse(self.atlas.navigator_real.execute_action(**kw, adapter_method="publish",
                                                                     approved_escalation_id=esc).success)
        direct = self.YT(vault=self.atlas.token_vault, account_reference_id=self.ref).publish(title="direto")
        self.assertEqual((direct.success, direct.delivery), (False, "NOT_SENT"))
        from bridge.execution.webhooks import WebhookDispatcher
        wh = WebhookDispatcher().deliver(url=f"http://127.0.0.1:{self.srv.server_address[1]}/h", payload={})
        self.assertEqual((wh.success, wh.delivery), (False, "NOT_SENT"))
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        gen = HttpGenerationProvider(api_key="k-local-teste",
                                     api_url=f"http://127.0.0.1:{self.srv.server_address[1]}/gen").call(
            operation="GENERATE", params={"estimated_cost": 0.01, "currency": "USD"})
        self.assertIn(BLOCK, gen.reason)
        self.assertEqual(_Svc.hits, [], "nenhum caminho alcançou o serviço")

    def test_unreadable_state_fails_closed_without_creating_db(self):
        missing = os.path.join(self.tmp, "sumiu.db")
        self.assertFalse(kill_switch.external_execution_allowed(missing))
        self.assertFalse(os.path.exists(missing))


# ------------------------------------------------------ 10–13 worker/scheduler
class WorkerSchedulerTests(_Base):
    def _run_on_mock_only_platform(self):
        r = self.runner()
        run = r.create_run(objective="o", requested_by="T", steps=[StepSpec(
            step_id="s", platform="plataforma_sem_conector", action="GET_EXECUTION_STATUS", adapter_method="x",
            params={})])
        return r, run

    def test_10_worker_recovers_job_after_crash(self):
        r, run = self._run_on_mock_only_platform()
        q = JobQueue(db_path=self.db)
        schedule_advance(q, run, tick=1)
        job = [j for j in q.all() if j.job_type == "navigator_run_advance"][0]
        q.try_start(job.job_id)                         # worker anterior "morreu" com o job RUNNING
        out = run_worker_cycle(db_path=self.db, runner=r, audit=self.app.facade.audit, stuck_job_seconds=0.0)
        self.assertEqual(out["recovered"], [job.job_id])
        self.assertEqual([p[0] for p in out["processed"]], [run])

    def test_11_two_workers_never_process_same_job_twice(self):
        r, run = self._run_on_mock_only_platform()
        q = JobQueue(db_path=self.db)
        for t in range(1, 6):
            schedule_advance(q, run, tick=t)
        results = []

        def go():
            results.append(run_worker_cycle(db_path=self.db, runner=r, worker_id=f"w{threading.get_ident()}"))
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        done = [j for j in q.all() if j.job_type == "navigator_run_advance" and j.state.value == "COMPLETED"]
        processed = sum(len(x["processed"]) for x in results)
        self.assertEqual(processed, len(done), "cada job concluído foi processado exatamente uma vez")

    def test_12_13_scheduler_no_duplicate_tick_and_restart(self):
        now = 2_000_000.0
        a = [x["status"] for x in Scheduler(self.db).tick(now=now)]
        b = [x["status"] for x in Scheduler(self.db).tick(now=now + 1)]        # restart na mesma janela
        self.assertEqual(a, ["SUCCESS", "SUCCESS"])
        self.assertEqual(b, ["ALREADY_CLAIMED", "ALREADY_CLAIMED"])
        results = []
        ts = [threading.Thread(target=lambda: results.append(Scheduler(self.db).tick(now=now + 120))) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ran = [x for rr in results for x in rr if x["status"] == "SUCCESS"]
        self.assertEqual(len(ran), 2, "cada janela roda uma única vez mesmo com 4 schedulers concorrentes")
        out = self.cli("scheduler", "--once")
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        out2 = self.cli("worker", "--once")
        self.assertEqual(out2.returncode, 0, out2.stderr[-400:])
        with db_module.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()[0], 1)


# ---------------------------------------------------------- 14–15 logging
class LoggingTests(unittest.TestCase):
    SECRETS = {"MGS_VAULT_MASTER_KEY": "vault-master-key-SECRETO-0123456789abcdef",
               "SESSION_SECRET": "session-secret-SECRETO-0123456789abcdef",
               "YOUTUBE_OAUTH_CLIENT_SECRET": "oauth-client-SECRETO-987654",
               "META_API_KEY": "meta-api-key-SECRETO-555555"}
    OWNER_PWD = "SenhaDoOwner-SECRETA-2026"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, self.SECRETS)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, "_mgs_operational_handler", False):
                root.removeHandler(h)
                h.close()

    def test_14_logs_persist_rotate_and_survive_restart(self):
        path = logging_setup.configure_logging(log_dir=self.dir, component="t", max_bytes=2000, backup_count=3)
        log = logging.getLogger("growth_studio.teste")
        log.info("linha antes do restart", extra={"correlation_id": "job-42"})
        logging_setup.configure_logging(log_dir=self.dir, component="t", max_bytes=2000, backup_count=3)  # "restart"
        for i in range(200):
            log.info("linha %s depois do restart", i)
        files = sorted(os.listdir(self.dir))
        self.assertLessEqual(len(files), 4, "retenção: arquivo atual + 3 rotacionados")
        allt = "".join(_read(os.path.join(self.dir, f)) for f in files)
        self.assertIn("depois do restart", allt)
        self.assertRegex(_read(path), r"\d{4}-\d\d-\d\d .* INFO \[growth_studio\.teste\] \[cid=")

    def test_15_secrets_never_reach_log_files(self):
        path = logging_setup.configure_logging(log_dir=self.dir, component="t")
        log = logging.getLogger("growth_studio.seguranca")
        for k, v in self.SECRETS.items():
            log.warning("config %s=%s carregada", k, v)
        log.error("Authorization: Bearer oauth-token-SECRETO-abcdefgh")
        log.error("login com password=%s", self.OWNER_PWD)
        log.error("chamada com api_key=%s", "chave-api-SECRETA-777")
        try:
            raise RuntimeError(f"falha com token {self.SECRETS['MGS_VAULT_MASTER_KEY']}")
        except RuntimeError:
            log.exception("exceção capturada")
        text = _read(path)
        for secret in list(self.SECRETS.values()) + [self.OWNER_PWD, "oauth-token-SECRETO-abcdefgh", "chave-api-SECRETA-777"]:
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)


# ----------------------------------------------------------- 16–18 custo
class CostTests(_Base):
    def test_16_usage_recorded_with_all_fields(self):
        g = cost_guard.CostGuard(self.db)
        g.set_limit(principal=self.owner, provider="llm_x", kind="per_call", amount=0.5, currency="USD")
        g.set_limit(principal=self.owner, provider="llm_x", kind="per_day", amount=1.0, currency="USD")
        self.assertTrue(g.authorize(provider="llm_x", service="chat", operation="gerar", estimated_cost=0.2,
                                    currency="USD", project="p1", campaign="c1")["authorized"])
        g.record_usage(provider="llm_x", service="chat", operation="gerar", success=True, currency="USD", cost=0.2,
                       input_tokens=1200, output_tokens=300, job_id="j1", project="p1", campaign="c1")
        with db_module.connect(self.db) as conn:
            row = dict(conn.execute("SELECT * FROM ai_api_usage").fetchone())
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["cost"], row["cost_status"], row["project"]),
                         (1200, 300, 0.2, "ESTIMATED", "p1"))
        self.assertEqual(g.authorize(provider="llm_x", service="chat", operation="gerar", estimated_cost=0.9,
                                     currency="USD")["code"], "LIMIT_EXCEEDED")

    def test_17_paid_provider_without_limit_is_blocked(self):
        g = cost_guard.CostGuard(self.db)
        self.assertEqual(g.authorize(provider="video_y", service="gen", operation="x", estimated_cost=0.01,
                                     currency="USD")["code"], "PAID_PROVIDER_NOT_AUTHORIZED")
        from core.agents.runtime.http_generation_provider import HttpGenerationProvider
        resp = HttpGenerationProvider(api_key="k-local", api_url=f"http://127.0.0.1:{self.srv.server_address[1]}/g").call(
            operation="GENERATE", params={"estimated_cost": 0.01, "currency": "USD"})
        self.assertFalse(resp.available)
        self.assertIn("PAID_PROVIDER_NOT_AUTHORIZED", resp.reason)
        self.assertEqual(_Svc.hits, [], "nenhuma chamada paga saiu")

    def test_18_unknown_cost_is_never_invented(self):
        g = cost_guard.CostGuard(self.db)
        g.set_limit(principal=self.owner, provider="llm_x", kind="per_call", amount=1.0, currency="USD")
        self.assertEqual(g.authorize(provider="llm_x", service="s", operation="o", estimated_cost=None,
                                     currency="USD")["code"], "COST_UNKNOWN")
        self.assertEqual(g.record_usage(usage_event_id="ev-1_0_1-404", provider="llm_x", service="s", operation="o", success=True)["cost_status"],
                         "COST_UNKNOWN")
        with db_module.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT cost FROM ai_api_usage").fetchone()[0])
        self.assertEqual(g.summary()["llm_x/None"]["calls_cost_unknown"], 1)
        with self.assertRaises(PermissionError):
            g.set_limit(principal=Principal(subject="a", role=Role.ATLAS, scopes={"*"}), provider="p", kind="per_call",
                        amount=1, currency="USD")


# ----------------------------------------------------------- 19–21 backup
class BackupTests(_Base):
    def test_19_21_cli_backup_hash_no_overwrite_retention_restore(self):
        bdir = os.path.join(self.tmp, "backups")
        self.app.facade.create_lead(self.owner, channel_id="c1", source="b", campaign="bk", idempotency_key="l1")
        outs = [self.cli("backup", "--backup-dir", bdir, "--retention", "2") for _ in range(3)]
        for o in outs:
            self.assertEqual(o.returncode, 0, o.stderr[-400:])
        reps = [json.loads(o.stdout.strip().splitlines()[-1]) for o in outs]
        self.assertEqual({r["mode"] for r in reps}, {"LOCAL_BACKUP_ONLY"})
        self.assertEqual(len({r["backup_file"] for r in reps}), 3, "nunca sobrescreve")
        files = sorted(f for f in os.listdir(bdir) if f.endswith(".db"))
        self.assertEqual(len(files), 2, "retenção 2")
        latest = os.path.join(bdir, files[-1])
        self.assertTrue(verify_backup(latest))
        c = sqlite3.connect(latest)
        try:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM leads WHERE campaign='bk'").fetchone()[0], 1)
        finally:
            c.close()
        with open(latest, "ab") as f:
            f.write(b"x")
        self.assertFalse(verify_backup(latest), "hash detecta adulteração do backup")
        self.assertEqual(run_backup(db_path=os.path.join(self.tmp, "nao_existe.db"), backup_dir=bdir)["status"], "FAILED")

    def test_20b_same_timestamp_never_overwrites(self):
        import datetime as _dt
        bdir = os.path.join(self.tmp, "col")
        fixed = _dt.datetime(2026, 9, 24, 12, 0, 0, 0, tzinfo=_dt.timezone.utc)

        class FrozenDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed
        with mock.patch("core.operations.backup.datetime.datetime", FrozenDT):
            a = run_backup(db_path=self.db, backup_dir=bdir, retention=0)
            b = run_backup(db_path=self.db, backup_dir=bdir, retention=0)
        self.assertEqual((a["status"], b["status"]), ("SUCCESS", "SUCCESS"))
        self.assertNotEqual(a["backup_file"], b["backup_file"], "colisão de timestamp nunca sobrescreve")
        self.assertTrue(verify_backup(a["backup_file"]) and verify_backup(b["backup_file"]))


# ------------------------------------------- 22–26 configuração / cookie / readiness
class ConfigTests(unittest.TestCase):
    def test_22_db_path_validation(self):
        self.assertEqual(config_check.check_db_path({})["status"], "REQUIRES_OWNER_CONFIGURATION")
        self.assertEqual(config_check.check_db_path({"DB_PATH": "/tmp/x.db"})["status"], "REQUIRES_OWNER_CONFIGURATION")
        self.assertEqual(config_check.check_db_path({"DB_PATH": os.path.join(ROOT, "data", "mgs.db")})["status"],
                         "REQUIRES_OWNER_CONFIGURATION")
        self.assertEqual(config_check.check_db_path({"DB_PATH": "/srv/growth/mgs.db"})["status"], "READY")

    def test_23_placeholder_and_weak_secrets_rejected(self):
        self.assertEqual(config_check.check_secret("SESSION_SECRET", {"SESSION_SECRET": "CHANGE_ME_GENERATE_A_RANDOM_SECRET"})
                         ["status"], "REJECTED_PLACEHOLDER")
        self.assertEqual(config_check.check_secret("SESSION_SECRET", {"SESSION_SECRET": "curta"})["status"], "REJECTED_WEAK")
        self.assertEqual(config_check.check_secret("MGS_VAULT_MASTER_KEY", {})["status"], "REQUIRES_OWNER_CONFIGURATION")

    def test_24_missing_key_never_silently_ephemeral_in_production(self):
        from bridge.execution.vault import TokenVault
        env = {k: v for k, v in os.environ.items() if k != "MGS_VAULT_MASTER_KEY"}
        env["APP_ENV"] = "production"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                TokenVault()
        with mock.patch.dict(os.environ, {"APP_ENV": "development"}):
            os.environ.pop("MGS_VAULT_MASTER_KEY", None)
            TokenVault()        # fora de produção: limitação honesta preservada (testes existentes)


class CookieAndReadinessTests(_Base):
    def _login_cookie(self, env):
        from api.server import make_handler
        with mock.patch.dict(os.environ, env):
            srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), make_handler(self.app))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                import http.client
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
                c.request("POST", "/api/login", body=json.dumps({"email": "o@t.com", "password": PWD}),
                          headers={"Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                c.close()
                return r.getheader("Set-Cookie") or ""
            finally:
                srv.shutdown()
                srv.server_close()

    def test_25_cookie_secure_in_production(self):
        prod = self._login_cookie({"APP_ENV": "production"})
        self.assertIn("Secure", prod)
        self.assertIn("HttpOnly", prod)
        self.assertIn("SameSite=Strict", prod)
        dev = self._login_cookie({"APP_ENV": "development", "COOKIE_SECURE": "0"})
        self.assertNotIn("Secure", dev)

    def test_26_readiness_reflects_states(self):
        bare = {"PATH": os.environ.get("PATH", "")}
        rep = operational_readiness(self.db, env=bare)
        self.assertTrue(rep["TECHNICALLY_READY"])
        self.assertFalse(rep["OPERATIONALLY_READY"])
        self.assertTrue(rep["EXTERNAL_INTEGRATION_PENDING"])
        oc = rep["operational_checks"]
        self.assertEqual((oc["worker"]["status"], oc["scheduler"]["status"], oc["backup"]["status"],
                          oc["tls"]["status"], oc["kill_switch"]["status"]),
                         ("NOT_RUNNING", "NOT_RUNNING", "NOT_CONFIGURED", "PUBLIC_EXPOSURE_NOT_READY", "READY"))
        bdir = os.path.join(self.tmp, "bk")
        run_backup(db_path=self.db, backup_dir=bdir)
        run_worker_cycle(db_path=self.db, runner=self.runner())
        Scheduler(self.db).tick()
        env = dict(bare, BACKUP_DIR=bdir, LOG_DIR=self.tmp, DB_PATH="/srv/growth/mgs.db",
                   MGS_VAULT_MASTER_KEY="k" * 44, SESSION_SECRET="s" * 48, OWNER_EMAIL="o@t.com",
                   OWNER_PASSWORD_HASH="pbkdf2$abc$def", BEHIND_TLS_PROXY="1", APP_ENV="production")
        rep2 = operational_readiness(self.db, env=env)
        self.assertTrue(rep2["OPERATIONALLY_READY"], rep2["operational_checks"])
        self.assertTrue(rep2["EXTERNAL_INTEGRATION_PENDING"], "credencial ausente não vira bug interno")
        before = _read(self.db, "rb")
        operational_readiness(self.db, env=env)
        self.assertEqual(_read(self.db, "rb"), before, "readiness é somente leitura")
        self.on()
        self.assertEqual(operational_readiness(self.db, env=env)["operational_checks"]["kill_switch"]["detail"],
                         "estado=ENABLED")


if __name__ == "__main__":
    unittest.main()
