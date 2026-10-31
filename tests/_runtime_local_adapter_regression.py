from atlas.execution.local_atlas_adapter import LocalAtlasAdapter

class FakeRunner:
    TOOL_NAME = "fake"
    def __init__(self):
        self.task = None
    def __call__(self, task):
        self.task = task
        return {"ok": True, "returncode": 0, "stdout": "ok", "stderr": ""}

runner = FakeRunner()
adapter = LocalAtlasAdapter(runner=runner)
r = adapter.execute_readonly(action="READ_FILE", path=r"C:\temp\probe.txt", task_id="reg-path")
assert r.success is True, r
assert "target:C:\\temp\\probe.txt" in runner.task.resources, runner.task.resources

runner2 = FakeRunner()
r2 = LocalAtlasAdapter(runner=runner2).execute_readonly(action="READ_FILE", target="A", path="B")
assert r2.success is False, r2
assert r2.data.get("error_type") == "ConflictingTarget", r2.data
assert runner2.task is None

runner3 = FakeRunner()
r3 = LocalAtlasAdapter(runner=runner3).execute_readonly(action="READ_FILE", target="A")
assert r3.success is True, r3
assert "target:A" in runner3.task.resources
print("LOCAL_ATLAS_ADAPTER_PATH_COMPAT=PASS")
