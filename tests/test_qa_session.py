from __future__ import annotations

import http.server
import json
import hashlib
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

from loopback_http import ThreadingLoopbackHTTPServer  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []
    secret = "qa-secret-canary-71"
    raw_responses: dict[str, bytes] = {}

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path in type(self).raw_responses:
            self.connection.sendall(type(self).raw_responses[self.path])
            self.close_connection = True
            return
        if self.path == "/slow":
            import time
            time.sleep(0.3)
            body = b'{"state":"ready"}'
            self.send_response(200)
        elif self.path == "/slow-drip":
            import time
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b'{"state":', b'"rea', b'dy"', b'}'):
                try:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.06)
                except OSError:
                    return
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                return
            return
        elif self.path == "/slow-headers":
            import time
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
            time.sleep(0.3)
            try:
                self.connection.sendall(b"Content-Length: 17\r\nConnection: close\r\n\r\n{\"state\":\"ready\"}")
            except OSError:
                pass
            return
        elif self.path in ("/close-delimited", "/close-delimited-stall"):
            body = b'{"state":"ready"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            if self.path.endswith("stall"):
                import time
                time.sleep(0.8)
            return
        elif self.path in ("/invalid/large-int", "/invalid/deep", "/invalid/infinity", "/invalid/overflow"):
            body = {
                "/invalid/large-int": b'{"value":' + b"9" * 5000 + b"}",
                "/invalid/deep": b'{"value":' + b"[" * 10000 + b"0" + b"]" * 10000 + b"}",
                "/invalid/infinity": b'{"value":Infinity}',
                "/invalid/overflow": b'{"value":1e9999}',
            }[self.path]
            self.send_response(200)
        elif self.path == "/invalid/rounded-decimal":
            body = b'{"value":9007199254740993.0}'
            self.send_response(200)
        elif self.path == "/objects/decimal-one":
            body = b'{"value":1.0}'
            self.send_response(200)
        elif self.path == "/objects/large-float":
            body = b'{"value":1000000000000000100.0}'
            self.send_response(200)
        elif self.path == "/objects/large-int":
            body = b'{"value":1000000000000000100}'
            self.send_response(200)
        elif self.path == "/objects/large-other-int":
            body = b'{"value":1000000000000000128}'
            self.send_response(200)
        elif self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b'{"state":"ready"}')
            self.close_connection = True
            return
        elif self.path == "/truncated-chunked":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b'{"state":"ready"}'
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            self.close_connection = True
            return
        elif self.path == "/objects/known":
            body = json.dumps({"id": "known-1", "state": "ready", "token": self.secret}).encode()
            self.send_response(200)
        elif self.path == "/objects/bool-true":
            body = b'{"value":true}'
            self.send_response(200)
        elif self.path == "/objects/bool-false":
            body = b'{"value":false}'
            self.send_response(200)
        elif self.path == "/objects/number-one":
            body = b'{"value":1}'
            self.send_response(200)
        elif self.path == "/objects/number-zero":
            body = b'{"value":0}'
            self.send_response(200)
        else:
            body = json.dumps({"error": "not found", "token": self.secret}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class QaSessionTests(unittest.TestCase):
    def setUp(self):
        Handler.hits = []
        Handler.raw_responses = {}
        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self.temp.name) / "project"
        self.project.mkdir()
        (self.project / ".codex").mkdir()
        (self.project / ".codex" / "devcrew.json").write_text(json.dumps({
            "schema_version": 1,
            "paths": {"context": "docs/context.md", "tasks": ".codex/tasks",
                      "sessions": ".codex/sessions", "memory": "memory", "checks": ".codex/checks"},
        }))
        self.scenario = self.project / "scenario.json"
        self.write_scenario()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def write_scenario(self, *, positive_status=200):
        self.scenario.write_text(json.dumps({
            "schema_version": 1,
            "profile": {"base_url": f"http://127.0.0.1:{self.server.server_port}", "timeout_seconds": 1},
            "scenarios": [
                {"id": "known-object", "path": "/objects/known", "expected_status": positive_status,
                 "expect_json": {"state": "ready", "id": "known-1"}},
                {"id": "missing-object", "path": "/objects/missing", "expected_status": 404,
                 "expect_json": {"error": "not found"}},
            ],
        }))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=10)

    def test_run_records_real_safe_api_evidence_and_read_only_reports(self):
        run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(Handler.hits, ["/objects/known", "/objects/missing"])
        run_id = payload["run_id"]
        listed = self.cli("qa", "list", "--project", self.project)
        reported = self.cli("qa", "report", "--project", self.project, "--run-id", run_id)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertEqual(Handler.hits, ["/objects/known", "/objects/missing"])
        self.assertIn(run_id, reported.stdout)
        stored = "\n".join(p.read_text(errors="ignore") for p in self.project.rglob("*") if p.is_file())
        self.assertNotIn(Handler.secret, stored)
        self.assertNotIn("token", stored)

    def test_unexpected_response_fails_and_unavailable_service_is_blocked(self):
        self.write_scenario(positive_status=201)
        run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "FAIL")
        self.assertEqual(len(Handler.hits), 2)

        self.server.shutdown()
        blocked = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["status"], "BLOCKED")

    def test_informational_response_then_invalid_final_status_is_readable(self):
        cases = (
            (b"HTTP/1.1 103 Early Hints\r\n\r\n"
             b"HTTP/1.1 malformed\r\n\r\n", "FAIL", None),
            (b"HTTP/1.1 103 Early Hints\r\n\r\n"
             b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n{\"state\":\"ready\"}", "PASS", 200),
            (b"HTTP/1.1 103 Early Hints\r\n\r\n"
             b"HTTP/1.1 600 Invalid\r\n\r\n", "FAIL", 600),
        )
        for response, expected, status_code in cases:
            with self.subTest(expected=expected, status_code=status_code):
                Handler.raw_responses["/raw/final-status"] = response
                scenario = json.loads(self.scenario.read_text())
                scenario["scenarios"][0]["path"] = "/raw/final-status"
                scenario["scenarios"][0]["expect_json"] = {"state": "ready"}
                self.scenario.write_text(json.dumps(scenario))

                run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
                self.assertEqual(run.returncode, 0, run.stderr)
                run_payload = json.loads(run.stdout)
                self.assertEqual(run_payload["status"], expected)
                listed = self.cli("qa", "list", "--project", self.project)
                reported = self.cli("qa", "report", "--project", self.project,
                                    "--run-id", run_payload["run_id"])
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertEqual(reported.returncode, 0, reported.stderr)
                observation = json.loads(reported.stdout)["evidence"]["observations"][0]
                if status_code is None:
                    self.assertEqual(observation["error"], "invalid_status")
                self.assertEqual(observation["status_code"], status_code)
                self.assertEqual(observation["outcome"], expected)

    def test_http_framing_matrix_fails_safely_and_reports_remain_readable(self):
        cases = {
            "invalid-length": b"Content-Length: invalid\r\n",
            "negative-length": b"Content-Length: -1\r\n",
            "conflicting-length": b"Content-Length: 17\r\nContent-Length: 18\r\n",
            "identical-length": b"Content-Length: 17\r\nContent-Length: 17\r\n",
            "length-and-transfer": b"Content-Length: 17\r\nTransfer-Encoding: chunked\r\n",
            "unsupported-transfer": b"Transfer-Encoding: gzip\r\n",
            "invalid-status": b"",
            "malformed-status": b"",
            "malformed-line": b"",
            "missing-status-separator": b"",
            "protocol-switch": b"",
            "chunked-space": b"Transfer-Encoding: chunked \r\n",
            "chunked-tab": b"Transfer-Encoding: \tchunked\t\r\n",
            "unframed-chunked-ows": b"Transfer-Encoding: chunked \t\r\n",
            "chunked-nbsp": "Transfer-Encoding: chunked\u00a0\r\n".encode(),
        }
        for case, headers in cases.items():
            with self.subTest(case=case):
                status = {"invalid-status": b"600 Not Valid", "malformed-status": b"099 Not Valid",
                          "malformed-line": b"bad status", "missing-status-separator": b"200",
                          "protocol-switch": b"101 Switching Protocols"}.get(case, b"200 OK")
                body = b'{"state":"ready"}'
                if case in ("chunked-space", "chunked-tab", "unframed-chunked-ows"):
                    if case in ("chunked-space", "chunked-tab"):
                        body = f"{len(body):X}\r\n".encode() + body + b"\r\n0\r\n\r\n"
                    Handler.raw_responses[f"/raw/{case}"] = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + headers
                        + b"Connection: close\r\n\r\n" + body)
                elif case == "malformed-line":
                    Handler.raw_responses[f"/raw/{case}"] = b"bad status\r\n\r\n"
                else:
                    Handler.raw_responses[f"/raw/{case}"] = (
                        b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                        + headers + b"Connection: close\r\n\r\n" + body)
                scenario = json.loads(self.scenario.read_text())
                scenario["scenarios"][0]["path"] = f"/raw/{case}"
                scenario["scenarios"][0]["expect_json"] = {"state": "ready"}
                self.scenario.write_text(json.dumps(scenario))
                run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
                self.assertEqual(run.returncode, 0, run.stderr)
                expected_run_status = "PASS" if case in ("identical-length", "chunked-space", "chunked-tab") else "FAIL"
                self.assertEqual(json.loads(run.stdout)["status"], expected_run_status)
                run_id = json.loads(run.stdout)["run_id"]
                listed = self.cli("qa", "list", "--project", self.project)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                report = self.cli("qa", "report", "--project", self.project, "--run-id", run_id)
                self.assertEqual(report.returncode, 0, report.stderr)
                observation = json.loads(report.stdout)["evidence"]["observations"][0]
                self.assertEqual(observation["outcome"], expected_run_status)
                if case in ("invalid-status", "malformed-status", "malformed-line", "missing-status-separator"):
                    self.assertEqual(observation["error"], "invalid_status")
                    self.assertEqual(observation["status_code"], 600 if case == "invalid-status" else 99 if case == "malformed-status" else None)
                elif case == "protocol-switch":
                    self.assertEqual(observation["error"], "invalid_response_framing")
                    self.assertEqual(observation["status_code"], 101)
                elif case == "unframed-chunked-ows":
                    self.assertIn(observation["error"], ("invalid_json", "incomplete_response"))
                else:
                    self.assertGreaterEqual(observation["status_code"], 100)
                    self.assertLessEqual(observation["status_code"], 599)

    def test_ipv6_host_header_uses_brackets(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import devcrew_qa
        finally:
            sys.path.pop(0)
        client, peer = socket.socketpair()
        seen = []

        def answer():
            seen.append(peer.recv(4096))
            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\nConnection: close\r\n\r\n{\"state\":\"ready\"}")
            peer.close()

        thread = threading.Thread(target=answer)
        thread.start()
        original = devcrew_qa.socket.create_connection
        devcrew_qa.socket.create_connection = lambda *_args, **_kwargs: client
        try:
            result = devcrew_qa._request(
                {"host": "::1", "port": 8080, "timeout_seconds": 1},
                {"path": "/probe", "expected_status": 200, "expect_json": {"state": "ready"}})
        finally:
            devcrew_qa.socket.create_connection = original
            client.close()
        thread.join(timeout=2)
        self.assertEqual(result, (200, {"state": "ready"}, None))
        self.assertIn(b"Host: [::1]:8080", seen[0])

    def test_normalized_snapshot_size_is_bounded_before_probes(self):
        segments = ".".join(["s" * 64] * 38)
        suffix = "z" * 40
        probes = []
        for i in range(20):
            fields = {f"key{j}.{segments}.{suffix}": "x" * 90 for j in range(20)}
            probes.append({"id": f"probe-{i}", "path": "/objects/known",
                           "expected_status": 404 if i == 19 else 200,
                           "expect_json": fields})
        scenario = {"schema_version": 1,
                    "profile": {"base_url": f"http://127.0.0.1:{self.server.server_port}"},
                    "scenarios": probes}
        raw = json.dumps(scenario).encode()
        self.assertLessEqual(len(raw), 1024 * 1024)


        normalized = (json.dumps({"schema_version": 1, "scenarios": probes},
                                 ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self.assertGreater(len(normalized), 1024 * 1024)
        self.scenario.write_bytes(raw)
        result = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("saved QA artifact exceeds the reader size limit", result.stderr)
        self.assertEqual(Handler.hits, [])

    def test_expected_json_distinguishes_booleans_from_numbers(self):
        data = json.loads(self.scenario.read_text())
        data["scenarios"].append({"id": "negative", "path": "/objects/missing",
                                  "expected_status": 404, "expect_json": {"error": "not found"}})
        data["scenarios"][1] = {"id": "typed-value", "path": "/objects/number-one",
                                "expected_status": 200, "expect_json": {"value": True}}
        self.scenario.write_text(json.dumps(data))
        true_vs_one = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(true_vs_one.stdout)["status"], "FAIL")

        data["scenarios"][1]["path"] = "/objects/number-zero"
        data["scenarios"][1]["expect_json"]["value"] = False
        self.scenario.write_text(json.dumps(data))
        false_vs_zero = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(false_vs_zero.stdout)["status"], "FAIL")

        data["scenarios"][1] = {"id": "typed-value", "path": "/objects/bool-true",
                                "expected_status": 200, "expect_json": {"value": True}}
        self.scenario.write_text(json.dumps(data))
        matching_true = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(matching_true.stdout)["status"], "PASS")

        data["scenarios"][1]["path"] = "/objects/bool-false"
        data["scenarios"][1]["expect_json"]["value"] = False
        self.scenario.write_text(json.dumps(data))
        matching_false = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(matching_false.stdout)["status"], "PASS")

    def test_rejects_unsafe_or_unknown_scenarios_without_network(self):
        data = json.loads(self.scenario.read_text())
        for unsafe_path in ("/objects/known?token=secret", "/../secret", "/objects/../secret"):
            data["scenarios"][0]["path"] = unsafe_path
            self.scenario.write_text(json.dumps(data))
            rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(Handler.hits, [])
        data["scenarios"][0]["path"] = "/objects/known"
        data["unexpected"] = True
        self.scenario.write_text(json.dumps(data))
        rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(Handler.hits, [])

    def test_requires_both_positive_and_negative_expected_coverage_before_requests(self):
        data = json.loads(self.scenario.read_text())
        data["scenarios"][1]["expected_status"] = 200
        self.scenario.write_text(json.dumps(data))
        rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(Handler.hits, [])

    def test_timeout_is_a_total_deadline_and_truncated_body_fails(self):
        data = json.loads(self.scenario.read_text())
        data["profile"]["timeout_seconds"] = 0.1
        data["scenarios"] = [
            {"id": "slow", "path": "/slow", "expected_status": 200, "expect_json": {"state": "ready"}},
            {"id": "negative", "path": "/objects/missing", "expected_status": 404, "expect_json": {"error": "not found"}},
        ]
        self.scenario.write_text(json.dumps(data))
        timed = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(timed.stdout)["status"], "BLOCKED")

        data["scenarios"][0]["path"] = "/slow-drip"
        self.scenario.write_text(json.dumps(data))
        drip = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(drip.stdout)["status"], "FAIL")

        data["scenarios"][0]["path"] = "/slow-headers"
        self.scenario.write_text(json.dumps(data))
        headers = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(headers.stdout)["status"], "FAIL")

        data["profile"]["timeout_seconds"] = 1
        data["scenarios"][0]["path"] = "/truncated-chunked"
        self.scenario.write_text(json.dumps(data))
        truncated = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(truncated.stdout)["status"], "FAIL")

    def test_close_delimited_eof_is_valid_only_before_deadline(self):
        data = json.loads(self.scenario.read_text())
        data["profile"]["timeout_seconds"] = 0.5
        data["scenarios"][0]["path"] = "/close-delimited"
        data["scenarios"][0]["expect_json"] = {"state": "ready"}
        self.scenario.write_text(json.dumps(data))
        timely = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        timely_result = json.loads(timely.stdout)
        timely_report = self.cli("qa", "report", "--project", self.project,
                                 "--run-id", timely_result["run_id"])
        self.assertEqual(timely_result["status"], "PASS", timely_report.stdout)

        data["scenarios"][0]["path"] = "/close-delimited-stall"
        data["profile"]["timeout_seconds"] = 0.5
        self.scenario.write_text(json.dumps(data))
        timed = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(json.loads(timed.stdout)["status"], "FAIL", timed.stderr)
        run_id = json.loads(timed.stdout)["run_id"]
        report = self.cli("qa", "report", "--project", self.project, "--run-id", run_id)
        observation = json.loads(report.stdout)["evidence"]["observations"][0]
        self.assertEqual(observation["error"], "incomplete_response")

    def test_invalid_json_decoder_inputs_are_recorded_as_fail(self):
        for path in ("/invalid/large-int", "/invalid/deep", "/invalid/infinity", "/invalid/overflow",
                     "/invalid/rounded-decimal"):
            with self.subTest(path=path):
                data = json.loads(self.scenario.read_text())
                data["scenarios"][0]["path"] = path
                data["scenarios"][0]["expect_json"] = {"value": 1}
                self.scenario.write_text(json.dumps(data))
                run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
                self.assertEqual(run.returncode, 0, run.stderr)
                payload = json.loads(run.stdout)
                self.assertEqual(payload["status"], "FAIL")
                self.assertRegex(payload["run_id"], r"^[0-9a-f]{32}$")
                report = self.cli("qa", "report", "--project", self.project,
                                  "--run-id", payload["run_id"])
                observation = json.loads(report.stdout)["evidence"]["observations"][0]
                if path != "/invalid/deep":
                    self.assertEqual(observation["error"], "invalid_json")

    def test_exact_supported_numbers_round_trip_and_keep_json_numeric_equivalence(self):
        data = json.loads(self.scenario.read_text())
        data["scenarios"][0]["path"] = "/objects/decimal-one"
        data["scenarios"][0]["expect_json"] = {"value": 1}
        self.scenario.write_text(json.dumps(data))
        run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(run.returncode, 0, run.stderr)
        run_id = json.loads(run.stdout)["run_id"]
        report = self.cli("qa", "report", "--project", self.project, "--run-id", run_id)
        self.assertEqual(report.returncode, 0, report.stderr)
        snapshot = self.project / ".codex/tasks/qa/snapshots" / run_id / "scenario.json"
        self.assertEqual(json.loads(snapshot.read_text())["scenarios"][0]["expect_json"], {"value": 1})

    def test_large_float_and_integer_match_only_the_same_decimal_value(self):
        scenarios = [
            ("/objects/large-float", 1000000000000000100.0, "PASS"),
            ("/objects/large-int", 1000000000000000100.0, "PASS"),
            ("/objects/large-int", 1000000000000000128, "FAIL"),
            ("/objects/large-float", 1000000000000000128, "FAIL"),
        ]
        for path, expected, status in scenarios:
            with self.subTest(path=path, expected=expected):
                data = json.loads(self.scenario.read_text())
                data["scenarios"][0]["path"] = path
                data["scenarios"][0]["expect_json"] = {"value": expected}
                self.scenario.write_text(json.dumps(data))
                run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)["status"], status)

        for actual, expected, should_match in ((1, 1.0, True), (1.0, 1, True), (True, 1, False), (1, True, False)):
            with self.subTest(actual=actual, expected=expected):
                sys.path.insert(0, str(ROOT / "scripts"))
                try:
                    import devcrew_qa
                    self.assertEqual(devcrew_qa._json_value_matches(actual, expected), should_match)
                finally:
                    sys.path.remove(str(ROOT / "scripts"))

    def test_unsupported_precision_in_expected_number_is_rejected_before_request(self):
        raw = self.scenario.read_text().replace('"state": "ready"', '"value": 1.0000000000000001')
        self.scenario.write_text(raw)
        run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(Handler.hits, [])

    def test_explicit_port_zero_is_rejected_before_network(self):
        data = json.loads(self.scenario.read_text())
        data["profile"]["base_url"] = "http://127.0.0.1:0"
        self.scenario.write_text(json.dumps(data))
        commands = [subprocess.Popen([sys.executable, "-B", str(CLI), "qa", "run-api", "--project",
                                      str(self.project), "--scenario", str(self.scenario)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        outputs = [process.communicate(timeout=15) for process in commands]
        self.assertTrue(all(process.returncode != 0 for process in commands), outputs)
        self.assertEqual(Handler.hits, [])

    def test_partial_coverage_and_unavailable_zero_coverage_never_pass(self):
        self.write_scenario(positive_status=201)
        partial = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(partial.returncode, 0, partial.stderr)
        partial_data = json.loads(partial.stdout)
        self.assertEqual(partial_data["status"], "FAIL")
        report = json.loads(self.cli("qa", "report", "--project", self.project,
                                     "--run-id", partial_data["run_id"]).stdout)
        self.assertEqual(report["result"]["completed_count"], 1)
        self.assertEqual(report["result"]["probe_count"], 2)

        self.server.shutdown()
        zero = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(zero.returncode, 0, zero.stderr)
        self.assertEqual(json.loads(zero.stdout)["status"], "BLOCKED")

    def test_missing_or_tampered_evidence_and_foreign_project_are_rejected(self):
        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        evidence = self.project / ".codex/checks/qa/runs" / run["run_id"] / "evidence.json"
        original = evidence.read_bytes()
        evidence.write_bytes(original + b" ")
        tampered = self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"])
        self.assertNotEqual(tampered.returncode, 0)
        evidence.unlink()
        missing = self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"])
        self.assertNotEqual(missing.returncode, 0)

        evidence.write_bytes(original)
        foreign = pathlib.Path(self.temp.name) / "foreign"
        foreign.mkdir()
        (foreign / ".codex").mkdir()
        (foreign / ".codex/devcrew.json").write_bytes((self.project / ".codex/devcrew.json").read_bytes())
        source_run = self.project / ".codex/checks/qa/runs" / run["run_id"]
        target_run = foreign / ".codex/checks/qa/runs" / run["run_id"]
        target_run.parent.mkdir(parents=True)
        shutil.copytree(source_run, target_run)
        result = self.cli("qa", "report", "--project", foreign, "--run-id", run["run_id"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project", result.stderr.lower())
        self.assertFalse((foreign / ".codex/checks/qa/index.json").exists())

    def test_unknown_schema_version_with_valid_fields_is_rejected_before_network(self):
        data = json.loads(self.scenario.read_text())
        data["schema_version"] = 2
        self.scenario.write_text(json.dumps(data))
        rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unsupported scenario schema version", rejected.stderr)
        self.assertEqual(Handler.hits, [])

    def test_missing_or_modified_markdown_report_is_rejected_by_list_and_report(self):
        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        report = self.project / ".codex/checks/qa/runs" / run["run_id"] / "report.md"
        report.unlink()
        self.assertNotEqual(self.cli("qa", "list", "--project", self.project).returncode, 0)
        self.assertNotEqual(self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"]).returncode, 0)

    def test_report_rejects_index_status_that_disagrees_with_result(self):
        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        index = self.project / ".codex/checks/qa/index.json"
        data = json.loads(index.read_text())
        data["runs"][0]["status"] = "FAIL"
        index.write_text(json.dumps(data))
        listed = self.cli("qa", "list", "--project", self.project)
        reported = self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"])
        self.assertNotEqual(listed.returncode, 0)
        self.assertNotEqual(reported.returncode, 0)

    def test_full_index_catalog_is_preflighted_before_network(self):
        runs = [{"run_id": f"{number:032x}", "status": "PASS",
                 "created_at": "2026-01-01T00:00:00Z", "result_sha256": "0" * 64}
                for number in range(37616)]
        catalog = {"schema_version": 1, "runs": runs}
        current = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        proposed = {**catalog, "runs": runs + [{"run_id": "f" * 32, "status": "BLOCKED",
                                                  "created_at": "2000-01-01T00:00:00Z",
                                                  "result_sha256": "0" * 64}]}
        next_catalog = (json.dumps(proposed, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        self.assertLessEqual(len(current), 8 * 1024 * 1024)
        self.assertGreater(len(next_catalog), 8 * 1024 * 1024)
        index = self.project / ".codex/checks/qa/index.json"
        index.parent.mkdir(parents=True)
        index.write_bytes(current)
        commands = [subprocess.Popen([sys.executable, "-B", str(CLI), "qa", "run-api", "--project",
                                      str(self.project), "--scenario", str(self.scenario)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(2)]
        outputs = [process.communicate(timeout=15) for process in commands]
        self.assertTrue(all(process.returncode != 0 for process in commands), outputs)
        self.assertEqual(Handler.hits, [])

    def test_valid_near_limit_scenario_exceeding_input_size_is_rejected_before_network(self):
        raw = json.dumps(json.loads(self.scenario.read_text()), ensure_ascii=False,
                         separators=(",", ":")).encode()
        self.assertLess(len(raw), 1024 * 1024)
        self.scenario.write_bytes(raw + b" " * (1024 * 1024 + 1 - len(raw)))
        rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("input exceeds the size limit", rejected.stderr)
        self.assertEqual(Handler.hits, [])

    def test_concurrent_runs_preserve_every_index_entry(self):
        commands = [subprocess.Popen([sys.executable, "-B", str(CLI), "qa", "run-api", "--project",
                                      str(self.project), "--scenario", str(self.scenario)], cwd=ROOT,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
        outputs = [process.communicate(timeout=15) for process in commands]
        self.assertTrue(all(process.returncode == 0 for process in commands), outputs)
        listed = self.cli("qa", "list", "--project", self.project)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)["runs"]), 4)

    def test_saved_artifacts_are_within_reader_limits(self):
        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        root = self.project / ".codex/checks/qa"
        for path in root.rglob("*.json"):
            self.assertLessEqual(path.stat().st_size, 8 * 1024 * 1024)
            json.loads(path.read_text())

    def test_symlink_state_and_symlink_scenario_are_rejected(self):
        alias = self.project / "scenario-link.json"
        alias.symlink_to(self.scenario)
        linked = self.cli("qa", "run-api", "--project", self.project, "--scenario", alias)
        self.assertNotEqual(linked.returncode, 0)
        self.assertEqual(Handler.hits, [])

        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        run_dir = self.project / ".codex/checks/qa/runs" / run["run_id"]
        evidence = run_dir / "evidence.json"
        external = pathlib.Path(self.temp.name) / "evidence-outside.json"
        external.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(external)
        rejected = self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"])
        self.assertNotEqual(rejected.returncode, 0)

    def test_symlink_configured_state_root_is_rejected_before_network(self):
        outside = pathlib.Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.project / ".codex/checks").symlink_to(outside, target_is_directory=True)
        rejected = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(Handler.hits, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_configured_paths_and_existing_project_state_are_preserved(self):
        config = json.loads((self.project / ".codex/devcrew.json").read_text())
        config["paths"]["tasks"] = ".codex/work-items"
        config["paths"]["checks"] = ".codex/receipts"
        config_bytes = json.dumps(config).encode()
        (self.project / ".codex/devcrew.json").write_bytes(config_bytes)
        sentinel = self.project / ".codex/receipts/existing-check.json"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b"preserve-check-receipt\n")
        # A real Git index is included in the preservation contract.
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "qa@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "QA"], check=True)
        tracked = self.project / "tracked.txt"
        tracked.write_text("checkout\n")
        subprocess.run(["git", "-C", str(self.project), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "fixture"], check=True)
        index_path = self.project / ".git/index"
        index_digest = hashlib.sha256(index_path.read_bytes()).hexdigest()
        run = self.cli("qa", "run-api", "--project", self.project, "--scenario", self.scenario)
        self.assertEqual(run.returncode, 0, run.stderr)
        run_id = json.loads(run.stdout)["run_id"]
        self.assertTrue((self.project / ".codex/work-items/qa/snapshots" / run_id / "scenario.json").is_file())
        self.assertTrue((self.project / ".codex/receipts/qa/runs" / run_id / "result.json").is_file())
        self.assertEqual(sentinel.read_bytes(), b"preserve-check-receipt\n")
        self.assertEqual((self.project / ".codex/devcrew.json").read_bytes(), config_bytes)
        self.assertEqual(hashlib.sha256(index_path.read_bytes()).hexdigest(), index_digest)
        self.assertEqual(tracked.read_text(), "checkout\n")

    def test_reports_are_read_only_and_saved_outputs_exclude_response_canary(self):
        listed = self.cli("qa", "list", "--project", self.project)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertFalse((self.project / ".codex/checks/qa").exists())
        run = json.loads(self.cli("qa", "run-api", "--project", self.project,
                                  "--scenario", self.scenario).stdout)
        root = self.project / ".codex/checks/qa/runs" / run["run_id"]
        report = self.cli("qa", "report", "--project", self.project, "--run-id", run["run_id"])
        self.assertEqual(report.returncode, 0, report.stderr)
        parsed_report = json.loads(report.stdout)
        self.assertEqual(parsed_report["markdown"], (root / "report.md").read_text())
        self.assertEqual(Handler.hits, ["/objects/known", "/objects/missing"])
        output_files = list(root.iterdir()) + list((self.project / ".codex/tasks/qa/snapshots" / run["run_id"]).iterdir())
        for path in output_files:
            self.assertNotIn(Handler.secret, path.read_text())
            self.assertNotIn('"token"', path.read_text())
        self.assertNotIn(Handler.secret, report.stdout)
        # The argv contains only CLI arguments and paths; raw HTTP data is never added.
        self.assertNotIn(Handler.secret, " ".join(["qa", "report", str(self.project), run["run_id"]]))


class HttpDecoderTests(unittest.TestCase):
    def request(self, response: bytes, expected_status: int = 200):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import devcrew_qa
        finally:
            sys.path.pop(0)
        client, peer = socket.socketpair()

        def answer():
            peer.recv(4096)
            peer.sendall(response)
            peer.close()

        thread = threading.Thread(target=answer)
        thread.start()
        original = devcrew_qa.socket.create_connection
        devcrew_qa.socket.create_connection = lambda *_args, **_kwargs: client
        try:
            result = devcrew_qa._request(
                {"host": "127.0.0.1", "port": 80, "timeout_seconds": 1},
                {"path": "/probe", "expected_status": expected_status,
                 "expect_json": {"state": "ready"}})
        finally:
            devcrew_qa.socket.create_connection = original
            client.close()
        thread.join(timeout=2)
        return result

    def test_http_decoder_status_headers_framing_and_completion_contracts(self):
        valid_body = b'{"state":"ready"}'
        cases = (
            (b"HTTP/1.0 200 \r\nContent-Length: 17\r\n\r\n" + valid_body, 200, None),
            (b"HTTP/1.1 200 OK\r\nContent-Length: " + b"0" * 80 + b"17\r\n\r\n" + valid_body,
             200, None),
            (b"HTTP/1.1  200 OK\r\nContent-Length: 17\r\n\r\n" + valid_body, None, "invalid_status"),
            (b"HTTP/1.1 200 OK\r\n folded: x\r\n\r\n", 200, "invalid_response_framing"),
            (b"HTTP/1.1 200 OK\r\nContent-Length : 17\r\n\r\n" + valid_body, 200,
             "invalid_response_framing"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 17, 17\r\n\r\n" + valid_body, 200,
             "invalid_response_framing"),
            (b"HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n", 200,
             "invalid_response_framing"),
            (b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 \r\nContent-Length: 17\r\n\r\n" + valid_body,
             200, None),
            (b"HTTP/1.1 101 Switching Protocols\r\n\r\n", 101, "invalid_response_framing"),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"11;foo=bar\r\n" + valid_body + b"\r\n0\r\nX-Note: ok\r\n\r\n", 200, None),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"00000011 ; foo = \"escaped\\\"quote\"\r\n" + valid_body
             + b"\r\n0\r\n\r\n", 200, None),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"11;foo=\"bad\\\r\n" + valid_body + b"\r\n0\r\n\r\n", 200,
             "invalid_response_framing"),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"11\r\n" + valid_body + b"\r\n0\r\nContent-Length: 17\r\n\r\n", 200,
             "invalid_response_framing"),
            (b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n", 204,
             "invalid_response_framing"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n{" , 200,
             "incomplete_response"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 999999999999999999999\r\n\r\n", 200,
             "response_too_large"),
            (b"HTTP/1.1 200 \r\nX-Long: " + b"x" * 65530 + b"\r\n\r\n", None,
             "invalid_response_framing"),
            (b"HTTP/1.1 200 \r\nX-One: " + b"x" * 32760 + b"\r\nX-Two: "
             + b"x" * 32760 + b"\r\n\r\n", None, "invalid_response_framing"),
        )
        for response, status, error in cases:
            with self.subTest(response=response[:70]):
                actual_status, payload, actual_error = self.request(response, status or 200)
                self.assertEqual(actual_status, status)
                self.assertEqual(actual_error, error)
                if error is None:
                    self.assertEqual(payload, {"state": "ready"})

    def test_chunk_extension_budget_excludes_size_lines_and_crlf(self):
        body = b'{"state":"ready"}' + b" " * (23028 - 17)
        response = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + b"".join(b"1\r\n" + bytes((byte,)) + b"\r\n" for byte in body)
                    + b"0\r\n\r\n")
        actual_status, payload, error = self.request(response)
        self.assertEqual((actual_status, payload, error), (200, {"state": "ready"}, None))

    def test_chunk_extension_budget_still_rejects_large_extensions(self):
        body = b'{"state":"ready"}'
        response = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + b"11;" + b"a" * 65537 + b"\r\n" + body + b"\r\n0\r\n\r\n")
        actual_status, _payload, error = self.request(response)
        self.assertEqual((actual_status, error), (200, "invalid_response_framing"))

if __name__ == "__main__":
    unittest.main()
