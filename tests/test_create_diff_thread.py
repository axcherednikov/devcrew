"""Exercise publication guards without contacting GitLab."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills/devcrew-review/scripts/create_diff_thread.sh"
SHAS = {"base": "a" * 40, "start": "b" * 40, "head": "c" * 40}
FAKE_GLAB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["GLAB_TEST_DIR"])
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] == ["auth", "status"]:
    sys.exit(0)
endpoint = args[-1]
if endpoint.endswith("/versions"):
    print((root / "versions.json").read_text())
elif "POST" in args:
    payload = json.load(sys.stdin)
    (root / "posted.json").write_text(json.dumps(payload))
    print(json.dumps({"id": "discussion-1"}))
elif endpoint.endswith("/discussions/discussion-1"):
    if os.environ.get("GLAB_TEST_READ_FAIL"):
        sys.exit(1)
    payload = json.loads((root / "posted.json").read_text())
    corrupt = os.environ.get("GLAB_TEST_CORRUPT")
    if corrupt:
        payload["position"][corrupt] = "d" * 40 if corrupt.endswith("sha") else 99
    print(json.dumps({"notes": [{"id": 1, "position": payload["position"]}]}))
else:
    raise SystemExit("unexpected API call")
"""


@unittest.skipUnless(shutil.which("jq"), "jq is required by the helper")
class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fake = self.root / "glab"
        fake.write_text(FAKE_GLAB)
        fake.chmod(0o755)
        self.env = dict(
            os.environ,
            PATH=str(self.root) + os.pathsep + os.environ["PATH"],
            GLAB_TEST_DIR=str(self.root),
        )
        self.version = {k + "_commit_sha": v for k, v in SHAS.items()}

    def run_helper(self, lines=("--new-line", "37"), omit=None):
        (self.root / "versions.json").write_text(json.dumps([self.version]))
        args = [
            "bash",
            str(SCRIPT),
            "--mr",
            "https://gitlab.example.test/group/project/-/merge_requests/42",
            "--body",
            "Проверь обработку ошибки.",
            "--new-path",
            "src/new.go",
            "--old-path",
            "src/old.go",
        ]
        for name, sha in SHAS.items():
            if name != omit:
                args.extend(["--" + name + "-sha", sha])
        return subprocess.run(args + list(lines), env=self.env, text=True, capture_output=True)

    def calls(self):
        p = self.root / "calls.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_added_deleted_and_context_positions(self):
        for flags, new, old in [
            (("--new-line", "37"), 37, None),
            (("--old-line", "35"), None, 35),
            (("--new-line", "37", "--old-line", "35"), 37, 35),
        ]:
            with self.subTest(flags=flags):
                result = self.run_helper(flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["status"], "verified")
                post = [args for args in self.calls() if "POST" in args][-1]
                self.assertEqual(post[post.index("--header") + 1], "Content-Type: application/json")
                self.assertEqual(post[post.index("--input") + 1], "-")
                self.assertEqual(post[post.index("--hostname") + 1], "gitlab.example.test")
                self.assertEqual(post[-1], "projects/group%2Fproject/merge_requests/42/discussions")
                pos = output["position"]
                self.assertEqual(pos.get("new_line"), new)
                self.assertEqual(pos.get("old_line"), old)
                for key, sha in SHAS.items():
                    self.assertEqual(pos[key + "_sha"], sha)

    def test_changed_revision_never_posts(self):
        for name in SHAS:
            with self.subTest(name=name):
                self.version = {k + "_commit_sha": v for k, v in SHAS.items()}
                self.version[name + "_commit_sha"] = "d" * 40
                result = self.run_helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("diff version changed", result.stderr)
                self.assertFalse(any("POST" in args for args in self.calls()))

    def test_missing_expected_revision_never_posts(self):
        for name in SHAS:
            with self.subTest(name=name):
                result = self.run_helper(omit=name)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("POST" in args for args in self.calls()))

    def test_mismatched_readback_is_not_verified(self):
        for field in ("base_sha", "start_sha", "head_sha", "new_line"):
            with self.subTest(field=field):
                self.env["GLAB_TEST_CORRUPT"] = field
                result = self.run_helper()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("discussion-1", result.stderr)
                self.assertNotIn('"verified"', result.stdout)

    def test_failed_readback_does_not_repeat_post(self):
        self.env["GLAB_TEST_READ_FAIL"] = "1"
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("discussion-1", result.stderr)
        self.assertEqual(sum("POST" in args for args in self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
