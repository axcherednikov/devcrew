import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AgentWaitingContract(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text()

    def test_shared_procedure_is_linked_from_execution_and_resume_paths(self):
        guide = ROOT / "skills/devcrew-task/references/agent-waiting.md"
        self.assertTrue(guide.exists(), "shared collaboration-agent waiting procedure is missing")
        task = self.read("skills/devcrew-task/SKILL.md")
        autopilot = self.read("skills/devcrew-autopilot/SKILL.md")
        coordination = self.read("skills/devcrew-coordinate/references/procedure.md")
        snapshot = self.read("skills/devcrew-task/references/session-snapshot.md")
        handoff = self.read("skills/devcrew/references/handoff.md")
        for name, content in [
            ("task", task),
            ("autopilot", autopilot),
            ("coordination", coordination),
            ("snapshot", snapshot),
            ("handoff", handoff),
        ]:
            with self.subTest(path=name):
                self.assertIn("agent-waiting.md", content)

    def test_guide_preserves_native_wait_and_delivery_contract(self):
        guide = self.read("skills/devcrew-task/references/agent-waiting.md")
        for required in (
            "wait_agent",
            "60,000",
            "timeout",
            "wait_threads",
            "clock.sleep",
            "terminal",
            "steering",
            "receipt",
            "UNKNOWN",
            "foreground",
            "60 seconds of that work",
            "Stop snapshots",
        ):
            with self.subTest(term=required):
                self.assertIn(required, guide)

    def test_evaluation_is_explicitly_simulated_and_cost_telemetry_unknown(self):
        source = self.read("evaluation/agent_waiting.py")
        report = self.read("evaluation/agent-waiting.md")
        self.assertIn("simulation", source.lower())
        self.assertIn("UNKNOWN", report)
        self.assertIn("30", report)
        self.assertIn("60", report)
        self.assertIn("timeout boundary", report)
        self.assertIn("resume", report)


if __name__ == "__main__":
    unittest.main()
