"""Exercise the runner against a local HTTP service; no real API credentials."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "code" / "run_api.py"
MODEL = "test-model-exact"
FAKE_SECRET = "test-only-private-key-do-not-log"


class FixtureProtocol:
    generated = 0

    @classmethod
    def iter_samples(cls, case):
        trace = case["trace"]
        for mode in ("whole", "step"):
            cls.generated += 1
            yield {
                "sample_id": trace["id"] + ":" + mode,
                "trace_id": trace["id"],
                "case": trace["case"],
                "mode": mode,
                "label": case["label"],
                "selected_step": case["selected_step"] if mode == "step" else None,
                "messages": [{"role": "user", "content": trace["body"] + ":" + mode}],
            }

    @staticmethod
    def parse_decision(content):
        try:
            value = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid JSON") from exc
        if value.get("decision") not in ("safe", "unsafe", "unknown"):
            raise ValueError("Invalid decision")
        return value["decision"], value.get("reason", "")


def response(decision="unsafe", *, finish="stop", content=None, model=MODEL):
    return {
        "id": "fixture-response-id",
        "system_fingerprint": "fixture-fingerprint",
        "model": model,
        "choices": [{
            "finish_reason": finish,
            "message": {
                "content": content if content is not None else json.dumps({
                    "decision": decision, "reason": "fixture decision",
                }),
                "reasoning_content": "fixture reasoning",
            },
        }],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


class LocalService:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.entered = threading.Event()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with service.lock:
                    service.requests.append((self.path, body))
                    index = len(service.requests) - 1
                    service.active += 1
                    service.max_active = max(service.active, service.max_active)
                service.entered.set()
                try:
                    status, value, *framing = service.handler(body, index)
                    framing = framing[0] if framing else None
                    data = json.dumps(value).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    if framing == "truncated_chunked":
                        self.send_header("Transfer-Encoding", "chunked")
                    else:
                        self.send_header("Content-Length", str(len(data) + (10 if framing == "truncated_length" else 0)))
                    self.end_headers()
                    if framing == "truncated_chunked":
                        self.wfile.write(f"{len(data) + 10:x}\r\n".encode() + data)
                    else:
                        self.wfile.write(data)
                    if framing:
                        self.close_connection = True
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with service.lock:
                        service.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1]) + "/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNNER.exists(), "API runner is not implemented")
        spec = importlib.util.spec_from_file_location("clashbench_test_runner", RUNNER)
        self.runner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.runner
        spec.loader.exec_module(self.runner)
        self.runner.RETRY_BASE_SECONDS = 0.001
        self.runner.PROGRESS_INTERVAL_SECONDS = 0.03
        FixtureProtocol.generated = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.data = self.folder / "cases.jsonl"
        self.output = self.folder / "results.jsonl"
        self.progress = self.folder / "progress.json"
        self.credentials = self.folder / "credentials.json"
        self.write_cases(1)

    def write_cases(self, count):
        with self.data.open("w") as stream:
            for i in range(count):
                stream.write(json.dumps({
                    "trace": {"id": f"trace-{i}", "case": f"case-{i}", "body": "FULL_CONTEXT_SENTINEL" * 30},
                    "reference": {"private_label_evidence": "not a model message"},
                    "selected_step": 1, "label": "unsafe" if i % 2 == 0 else "safe",
                }) + "\n")

    def args(self, service, *extra):
        self.credentials.write_text(json.dumps({"base_url": service.url, "api_key": FAKE_SECRET}))
        self.credentials.chmod(0o600)
        return [
            "--model", MODEL, "--credentials", str(self.credentials),
            "--data", str(self.data), "--output", str(self.output),
            "--progress", str(self.progress), "--concurrency", "1",
            "--max-tokens", "256", "--timeout", "2", "--attempts", "3",
            *extra,
        ]

    def rows(self):
        return [json.loads(line) for line in self.output.read_text().splitlines() if line.strip()]

    def run_main(self, args):
        return self.runner.main(args, protocol_module=FixtureProtocol)

    def test_success_retains_raw_response_and_sends_full_input(self):
        with LocalService(lambda body, i: (200, response())) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 0)
        row, = self.rows()
        self.assertEqual(row["status"], "predicted")
        self.assertEqual(row["decision"], "unsafe")
        self.assertEqual(row["requested_model"], MODEL)
        self.assertEqual(row["returned_model"], MODEL)
        self.assertEqual(row["response_id"], "fixture-response-id")
        self.assertEqual(row["system_fingerprint"], "fixture-fingerprint")
        self.assertEqual(row["usage"]["total_tokens"], 10)
        self.assertEqual(row["reasoning"], "fixture reasoning")
        self.assertEqual(json.loads(row["content"])["decision"], "unsafe")
        self.assertEqual(row["reference"]["label"], "unsafe")
        self.assertGreaterEqual(row["latency_seconds"], 0)
        path, payload = service.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["messages"][0]["content"], "FULL_CONTEXT_SENTINEL" * 30 + ":whole")
        self.assertNotIn(FAKE_SECRET, self.output.read_text() + self.progress.read_text())

    def test_base_url_without_v1_uses_openai_v1_endpoint(self):
        with LocalService(lambda body, i: (200, response())) as service:
            args = self.args(service, "--limit", "1")
            self.credentials.write_text(json.dumps({"base_url": service.url.removesuffix("/v1"), "api_key": FAKE_SECRET}))
            rc = self.run_main(args)
        self.assertEqual(rc, 0)
        self.assertEqual(service.requests[0][0], "/v1/chat/completions")

    def test_no_proxy_bypasses_environment_proxy_without_changing_environment(self):
        with LocalService(lambda body, i: (200, response())) as service, LocalService(
                lambda body, i: (502, {"error": "fixture proxy failure"})) as proxy:
            proxy_url = proxy.url.removesuffix("/v1")
            proxy_environment = {
                "http_proxy": proxy_url, "https_proxy": proxy_url,
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
                "all_proxy": "", "ALL_PROXY": "", "no_proxy": "", "NO_PROXY": "",
            }
            with patch.dict(os.environ, proxy_environment), patch("urllib.request._opener", None):
                rc = self.run_main(self.args(service, "--limit", "1", "--no-proxy"))
                self.assertEqual({key: os.environ[key] for key in proxy_environment}, proxy_environment)
        self.assertEqual(rc, 0)
        self.assertEqual(len(service.requests), 1)
        self.assertEqual(len(proxy.requests), 0)
        self.assertEqual(self.rows()[0]["status"], "predicted")

    def test_default_request_still_uses_environment_proxy(self):
        with LocalService(lambda body, i: (200, response())) as service, LocalService(
                lambda body, i: (502, {"error": "fixture proxy failure"})) as proxy:
            proxy_url = proxy.url.removesuffix("/v1")
            proxy_environment = {
                "http_proxy": proxy_url, "https_proxy": proxy_url,
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
                "all_proxy": "", "ALL_PROXY": "", "no_proxy": "", "NO_PROXY": "",
            }
            with patch.dict(os.environ, proxy_environment), patch("urllib.request._opener", None):
                rc = self.run_main(self.args(service, "--limit", "1", "--attempts", "1"))
        self.assertEqual(rc, 1)
        self.assertEqual(len(service.requests), 0)
        self.assertEqual(len(proxy.requests), 1)
        self.assertEqual(self.rows()[0]["http_status"], 502)

    def test_parse_and_http_failures_retry_and_append_every_attempt(self):
        def handler(body, i):
            if i == 0:
                return 200, response(content="unparseable fixture")
            if i == 1:
                return 429, {"error": {"message": "Retry " + FAKE_SECRET}}
            return 200, response()

        with LocalService(handler) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 0)
        self.assertEqual([r["status"] for r in self.rows()], ["parse_error", "request_error", "predicted"])
        self.assertEqual([r["attempt"] for r in self.rows()], [1, 2, 3])
        self.assertEqual(len(service.requests), 3)
        progress = json.loads(self.progress.read_text())
        self.assertEqual(progress["samples_completed"], 1)
        self.assertEqual(progress["attempts"], 3)
        self.assertEqual(progress["usage"]["total_tokens"], 20)
        self.assertNotIn(FAKE_SECRET, self.output.read_text() + self.progress.read_text())

    def test_resume_skips_only_matching_model_valid_binary_decisions(self):
        with LocalService(lambda body, i: (200, response("unsafe" if i == 0 else "unknown"))) as service:
            first = self.run_main(self.args(service, "--attempts", "1"))
        self.assertEqual(first, 1)
        with self.output.open("a") as stream:
            stream.write(json.dumps({"sample_id": "trace-0:step", "requested_model": "another-model", "status": "predicted", "decision": "safe"}) + "\n")
        with LocalService(lambda body, i: (200, response("safe"))) as service:
            second = self.run_main(self.args(service, "--attempts", "1"))
        self.assertEqual(second, 0)
        self.assertEqual(len(service.requests), 1)
        self.assertTrue(service.requests[0][1]["messages"][0]["content"].endswith(":step"))
        progress = json.loads(self.progress.read_text())
        self.assertEqual(progress["samples_skipped_resume"], 1)
        self.assertEqual(progress["samples_completed"], 2)

    def test_incomplete_response_retries_and_continues_the_batch(self):
        def handler(body, i):
            if i == 0:
                return 200, response(content=FAKE_SECRET), "truncated_length"
            return 200, response()

        with LocalService(handler) as service:
            rc = self.run_main(self.args(service))
        self.assertEqual(rc, 0)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["request_error", "predicted", "predicted"])
        self.assertEqual([r["attempt"] for r in rows], [1, 2, 1])
        self.assertEqual(rows[0]["http_status"], 200)
        self.assertIn("IncompleteRead", rows[0]["error"])
        self.assertEqual(len(service.requests), 3)
        progress = json.loads(self.progress.read_text())
        self.assertEqual(progress["state"], "finished")
        self.assertEqual(progress["samples_completed"], 2)
        self.assertEqual(progress["samples_failed"], 0)
        self.assertNotIn(FAKE_SECRET, self.output.read_text() + self.progress.read_text())

    def test_incomplete_http_error_body_is_retryable_and_retained(self):
        def handler(body, i):
            if i == 0:
                return 400, {"error": {"message": FAKE_SECRET}}, "truncated_chunked"
            return 200, response()

        with LocalService(handler) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 0)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["request_error", "predicted"])
        self.assertEqual([r["attempt"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["http_status"], 400)
        self.assertIn("IncompleteRead", rows[0]["error"])
        self.assertEqual(len(service.requests), 2)
        self.assertNotIn(FAKE_SECRET, self.output.read_text() + self.progress.read_text())

    def test_work_queue_keeps_only_concurrency_bound_in_memory(self):
        self.write_cases(100)
        first_observed = []
        first_response = threading.Event()

        def handler(body, i):
            if i == 0:
                time.sleep(0.04)
                first_observed.append(FixtureProtocol.generated)
                first_response.set()
            else:
                first_response.wait(2)
                time.sleep(0.04)
            return 200, response()

        with LocalService(handler) as service:
            rc = self.run_main(self.args(service, "--concurrency", "3", "--limit", "12"))
        self.assertEqual(rc, 0)
        self.assertLessEqual(first_observed[0], 3)
        self.assertLessEqual(service.max_active, 3)
        self.assertGreater(service.max_active, 1)
        self.assertEqual(len(service.requests), 12)
        self.assertEqual(len(self.rows()), 12)
        self.assertEqual(json.loads(self.progress.read_text())["samples_seen"], 12)

    def test_truncated_and_unknown_answers_are_incomplete(self):
        with LocalService(lambda body, i: (200, response("safe", finish="length") if i == 0 else response("unknown"))) as service:
            rc = self.run_main(self.args(service, "--attempts", "1"))
        self.assertEqual(rc, 1)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["output_truncated", "unknown"])
        self.assertIsNone(rows[0]["decision"])
        self.assertEqual(rows[1]["decision"], "unknown")
        progress = json.loads(self.progress.read_text())
        self.assertEqual(progress["samples_completed"], 0)
        self.assertEqual(progress["samples_failed"], 1)
        self.assertEqual(progress.get("samples_abstained", 0), 1)

    def test_context_overflow_is_retained_without_truncating_or_retrying(self):
        with LocalService(lambda body, i: (400, {"error": {"code": "context_length_exceeded", "message": FAKE_SECRET}})) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 1)
        self.assertEqual(len(service.requests), 1)
        row, = self.rows()
        self.assertEqual(row["status"], "context_overflow")
        self.assertIsNone(row["decision"])
        self.assertNotIn(FAKE_SECRET, self.output.read_text())

    def test_unknown_abstention_is_not_retried_within_the_same_run(self):
        with LocalService(lambda body, i: (200, response("unknown"))) as service:
            rc = self.run_main(self.args(service, "--limit", "1", "--attempts", "3"))
        self.assertEqual(rc, 1)
        self.assertEqual(len(service.requests), 1)
        row, = self.rows()
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["decision"], "unknown")
        progress = json.loads(self.progress.read_text())
        self.assertEqual(progress.get("samples_abstained", 0), 1)
        self.assertEqual(progress["samples_failed"], 0)

    def test_error_redaction_happens_before_error_text_is_shortened(self):
        with LocalService(lambda body, i: (400, {"error": {"message": "x" * 1960 + FAKE_SECRET}})) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 1)
        self.assertNotIn("test-only-private", self.output.read_text())

    def test_provider_returned_model_version_is_retained(self):
        with LocalService(lambda body, i: (200, response(model="test-model-exact-20261013"))) as service:
            rc = self.run_main(self.args(service, "--limit", "1"))
        self.assertEqual(rc, 0)
        row, = self.rows()
        self.assertEqual(row["returned_model"], "test-model-exact-20261013")
        self.assertEqual(row["requested_model"], MODEL)
        self.assertEqual(row["decision"], "unsafe")

    @unittest.skipUnless(os.name == "posix", "POSIX signal lifecycle")
    def test_sigterm_stops_submission_and_flushes_progress(self):
        self.write_cases(50)
        stage = self.folder / "stage"
        stage.mkdir()
        shutil.copy(RUNNER, stage / "run_api.py")
        (stage / "protocol.py").write_text('''import json
def iter_samples(case):
    for mode in ("whole", "step"):
        yield {"sample_id": case["trace"]["id"]+":"+mode, "trace_id": case["trace"]["id"], "case": "signal", "mode": mode, "label": case["label"], "selected_step": case["selected_step"], "messages": [{"role": "user", "content": "fixture"}]}
def parse_decision(content):
    value = json.loads(content)
    return value["decision"], value["reason"]
''')

        def handler(body, i):
            time.sleep(0.3)
            return 200, response()

        with LocalService(handler) as service:
            args = self.args(service, "--concurrency", "2", "--attempts", "1")
            process = subprocess.Popen([sys.executable, str(stage / "run_api.py"), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            self.assertTrue(service.entered.wait(4), "runner never submitted a request")
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, stderr.decode())
        self.assertLessEqual(len(service.requests), 2)
        self.assertEqual(json.loads(self.progress.read_text())["state"], "interrupted")
        self.assertNotIn(FAKE_SECRET.encode(), stdout + stderr)


if __name__ == "__main__":
    unittest.main()
