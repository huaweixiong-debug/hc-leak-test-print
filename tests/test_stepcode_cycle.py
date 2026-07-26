import unittest

from stepcode_cycle import StepCodeCycleDetector


class StepCodeCycleDetectorTests(unittest.TestCase):
    def test_exact_idle_to_fill_edge_starts_once(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(65535))
        self.assertTrue(detector.observe(4))
        self.assertFalse(detector.observe(4))
        self.assertFalse(detector.observe(4))

    def test_starting_while_fill_is_active_does_not_start(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(4))
        self.assertFalse(detector.observe(4))

    def test_communication_failure_breaks_the_edge(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(65535))
        self.assertFalse(detector.observe(None))
        self.assertFalse(detector.observe(4))

    def test_idle_after_failure_rearms_the_detector(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(None))
        self.assertFalse(detector.observe(65535))
        self.assertTrue(detector.observe(4))

    def test_next_cycle_requires_a_new_idle_value(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(65535))
        self.assertTrue(detector.observe(4))
        self.assertFalse(detector.observe(0))
        self.assertFalse(detector.observe(4))
        self.assertFalse(detector.observe(65535))
        self.assertTrue(detector.observe(4))

    def test_only_stepcode_four_can_start(self):
        detector = StepCodeCycleDetector()

        self.assertFalse(detector.observe(65535))
        self.assertFalse(detector.observe(5))
        self.assertFalse(detector.observe(6))


if __name__ == "__main__":
    unittest.main()
