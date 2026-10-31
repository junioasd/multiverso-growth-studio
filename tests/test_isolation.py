import unittest

from core.isolation.isolation import IsolationGuard, IsolationViolation, ResourceScope


class IsolationGuardTests(unittest.TestCase):
    def test_owner_scope_bypasses_check(self):
        IsolationGuard.check({"*"}, ResourceScope(channel_id="chan_1"))  # não deve levantar

    def test_matching_channel_scope_allowed(self):
        IsolationGuard.check({"channel:chan_1"}, ResourceScope(channel_id="chan_1"))

    def test_mismatched_channel_scope_denied(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"channel:chan_1"}, ResourceScope(channel_id="chan_2"))

    def test_mismatched_project_scope_denied(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"project:proj_1"}, ResourceScope(project_id="proj_2"))

    def test_assert_same_channel_ok(self):
        IsolationGuard.assert_same_channel("chan_1", "chan_1")

    def test_assert_same_channel_violation(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.assert_same_channel("chan_1", "chan_2")

    def test_cross_project_access_denied(self):
        with self.assertRaises(IsolationViolation):
            IsolationGuard.check({"project:proj_A"}, ResourceScope(project_id="proj_B"))


if __name__ == "__main__":
    unittest.main()
