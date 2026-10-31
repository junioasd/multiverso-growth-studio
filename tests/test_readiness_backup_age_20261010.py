"""Backup freshness gate regression; uses isolated temporary SQLite only."""
import datetime,json,os,tempfile,unittest
from core.models import db as db_module
from core.operations.readiness import operational_readiness

UTC=datetime.timezone.utc
BASE=datetime.datetime(2026,10,10,21,15,tzinfo=UTC)
class BackupFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory(prefix="mgs_backup_gate_test_")
        self.db=os.path.join(self.dir.name,"mgs.db")
        self.bk=os.path.join(self.dir.name,"backup")
        os.makedirs(self.bk)
        db_module.init_db(self.db)
        self.env={"BACKUP_DIR":self.bk,"LOG_DIR":self.dir.name,"APP_ENV":"development"}
    def tearDown(self):
        self.dir.cleanup()
    def make_report(self,when,status="SUCCESS"):
        path=os.path.join(self.bk,"backup_report.jsonl")
        with open(path,"w",encoding="utf-8") as f:
            json.dump({"status":status,"started_at":when,"mode":"LOCAL_BACKUP_ONLY"},f)
            f.write("\n")
    def status(self):
        rep=operational_readiness(self.db,env=self.env,now=BASE.timestamp(),backup_max_age=36*3600)
        return rep["operational_checks"]["backup"]["status"]
    def test_recent_backup_ready(self):
        self.make_report((BASE-datetime.timedelta(hours=1)).strftime("%Y%m%dT%H%M%S%fZ"))
        self.assertEqual(self.status(),"READY")
    def test_stale_backup_rejected(self):
        self.make_report((BASE-datetime.timedelta(days=7)).strftime("%Y%m%dT%H%M%S%fZ"))
        self.assertEqual(self.status(),"NOT_READY")
    def test_future_backup_rejected(self):
        self.make_report((BASE+datetime.timedelta(days=1)).strftime("%Y%m%dT%H%M%S%fZ"))
        self.assertEqual(self.status(),"NOT_READY")
    def test_invalid_timestamp_rejected(self):
        self.make_report("n/a")
        self.assertEqual(self.status(),"NOT_READY")
    def test_failed_backup_rejected(self):
        self.make_report((BASE-datetime.timedelta(minutes=5)).strftime("%Y%m%dT%H%M%S%fZ"),status="FAILED")
        self.assertEqual(self.status(),"NOT_READY")
if __name__=="__main__":unittest.main(verbosity=2)
