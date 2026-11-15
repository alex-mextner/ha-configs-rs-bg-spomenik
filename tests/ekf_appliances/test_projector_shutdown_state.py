"""A shutdown latch is released only by fresh explicit HDMI OFF evidence."""
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[2] / "custom_components/ekf_appliances/projector_shutdown.py"
SPEC = importlib.util.spec_from_file_location("shutdown_state_under_test", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ShutdownStateTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.latch = module.ProjectorShutdown(changed=lambda: self.events.append(self.latch.active))

    def test_off_request_sets_latch_synchronously_only_when_observed_on(self):
        for observed in (None, False):
            self.latch.request_off(observed, 100)
            self.assertFalse(self.latch.active)
        self.latch.request_off(True, 100)
        self.assertTrue(self.latch.active)
        self.assertEqual(self.events, [True])

    def test_cached_off_unknown_unavailable_and_timeout_do_not_clear(self):
        self.latch.request_off(True, 100)
        for value, stamp, fresh, restored in [(False,99,True,False), (False,100,True,False),
                                             (None,101,True,False), (False,101,False,False),
                                             (False,101,True,True), (True,101,True,False)]:
            self.latch.observe(value, stamp, fresh=fresh, restored=restored)
            self.assertTrue(self.latch.active)
        self.latch.observe(None, 1000, fresh=False)
        self.assertTrue(self.latch.active)

    def test_only_new_explicit_fresh_hdmi_off_clears(self):
        self.latch.request_off(True, 100)
        self.latch.observe(False, 100.001, fresh=True)
        self.assertFalse(self.latch.active)
        self.assertEqual(self.events, [True, False])

    def test_restored_active_latch_requires_new_poll_after_reload(self):
        self.latch.restore({"active":True, "requested_at":100}, 200)
        self.assertTrue(self.latch.active)
        self.latch.observe(False, 199.99, fresh=True)
        self.assertTrue(self.latch.active)
        self.latch.observe(None, 201, fresh=False)
        self.assertTrue(self.latch.active)
        self.latch.observe(False, 202, fresh=True)
        self.assertFalse(self.latch.active)

    def test_shutdown_state_serializes_no_runtime_or_credential_data(self):
        self.latch.request_off(True, 100)
        self.assertEqual(self.latch.serialize(), {"active":True, "requested_at":100})


if __name__ == "__main__":
    unittest.main()
