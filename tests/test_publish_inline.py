import importlib.util
import json
import subprocess
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/devcrew-review/scripts/publish_inline.py"
spec = importlib.util.spec_from_file_location("publish_inline", SCRIPT)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class InlinePublication(unittest.TestCase):
    def setUp(self):
        self.args = Namespace(
            url="https://example.test/acme/docs/pull/12",
            path="docs/new.txt",
            old_path="docs/old.txt",
            old_line=None,
            new_line=7,
            base_sha="a" * 40,
            head_sha="b" * 40,
            start_sha="c" * 40,
        )
        self.pr = {"base": {"sha": self.args.base_sha}, "head": {"sha": self.args.head_sha}}
        self.body = "Проблема\n\nEvidence from source."

    def test_provider_cli_takes_unicode_body_on_standard_input(self):
        text = {"body": self.body}
        completed = subprocess.CompletedProcess([], 0, b'{"id": 43}', b"")
        with patch.object(publisher.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                publisher.api("gh", "host.test", "repos/a/b/pulls/1/comments", text), {"id": 43}
            )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ["gh", "api", "--hostname", "host.test"])
        self.assertIn("POST", argv)
        self.assertNotIn(self.body, argv)
        self.assertEqual(json.loads(run.call_args.kwargs["input"].decode("utf-8")), text)

    def test_github_deletion_and_context_follow_correct_diff_side(self):
        for old, new, side in [(4, None, "LEFT"), (4, 7, "RIGHT"), (None, 7, "RIGHT")]:
            with self.subTest(side=side, old=old):
                self.args.old_line, self.args.new_line = old, new
                pos = {
                    "path": self.args.path,
                    "commit_id": self.args.head_sha,
                    "side": side,
                    "line": new or old,
                }
                saved = {"id": 43, "body": self.body, **pos}
                with patch.object(
                    publisher, "api", side_effect=[self.pr, {"id": 43}, saved]
                ) as api:
                    self.assertEqual(publisher.publish(self.args, self.body)["status"], "verified")
                    calls = api.call_args_list
                    self.assertEqual(
                        calls[1].args,
                        (
                            "gh",
                            "example.test",
                            "repos/acme/docs/pulls/12/comments",
                            {"body": self.body, **pos},
                        ),
                    )
                    self.assertEqual(calls[2].args[-1], "repos/acme/docs/pulls/comments/43")

    def test_gitlab_version_and_rename_are_verified(self):
        self.args.url = "https://code.example.test/group/sub/project/-/merge_requests/5"
        version = {
            "base_commit_sha": self.args.base_sha,
            "start_commit_sha": self.args.start_sha,
            "head_commit_sha": self.args.head_sha,
        }
        pos = {
            "position_type": "text",
            "base_sha": self.args.base_sha,
            "start_sha": self.args.start_sha,
            "head_sha": self.args.head_sha,
            "old_path": self.args.old_path,
            "new_path": self.args.path,
            "new_line": 7,
        }
        saved = {"id": "ff21", "notes": [{"position": pos, "body": self.body}]}
        with patch.object(publisher, "api", side_effect=[[version], {"id": "ff21"}, saved]) as api:
            self.assertEqual(publisher.publish(self.args, self.body)["position"], pos)
            self.assertIn("group%2Fsub%2Fproject", api.call_args_list[1].args[2])
            self.assertEqual(api.call_args_list[1].args[3]["position"], pos)

    def test_stale_version_never_writes(self):
        self.args.head_sha = "f" * 40
        with patch.object(publisher, "api", return_value=self.pr) as api:
            with self.assertRaisesRegex(publisher.PublicationError, "diff version changed"):
                publisher.publish(self.args, self.body)
            self.assertEqual(api.call_count, 1)

    def test_readback_mismatch_retains_id_and_never_retries(self):
        with patch.object(
            publisher, "api", side_effect=[self.pr, {"id": 45}, {"id": 45, "body": "other"}]
        ) as api:
            with self.assertRaisesRegex(
                publisher.PublicationError, "created 45 but readback failed"
            ):
                publisher.publish(self.args, self.body)
            self.assertEqual(api.call_count, 3)

    def test_explicit_target_and_position_required_before_api_access(self):
        with patch.object(publisher, "api") as api:
            for value in (
                "https://example.test/org/repo/issues/2",
                "https://user:secret@example.test/a/b/pull/3",
            ):
                self.args.url = value
                with self.assertRaises(ValueError):
                    publisher.publish(self.args, self.body)
            api.assert_not_called()
