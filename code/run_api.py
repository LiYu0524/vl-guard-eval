#!/usr/bin/env python3
"""Stream fixed evaluation samples through an OpenAI-compatible HTTP endpoint."""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from http.client import HTTPException
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen
import uuid


RETRY_BASE_SECONDS = 1.0
PROGRESS_INTERVAL_SECONDS = 30.0
VALID_DECISIONS = {"safe", "unsafe"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sanitize(value, secret):
    """Remove the external bearer value from retained data and error text."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: sanitize(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item, secret) for item in value]
    return value


def load_credentials(path):
    path = Path(path)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Credential file must be private (mode 0600)")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Credential configuration must be a JSON object")
    base = value.get("base_url")
    key = value.get("api_key")
    if not isinstance(base, str) or not isinstance(key, str) or not key.strip():
        raise ValueError("Credential configuration needs base_url and api_key")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be an HTTP(S) endpoint without URL credentials")
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        endpoint = base
    elif base.endswith("/v1"):
        endpoint = base + "/chat/completions"
    else:
        endpoint = base + "/v1/chat/completions"
    return {"endpoint": endpoint, "api_key": key}


def load_extra_body(path):
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Extra body must be a JSON object")
    if {"model", "messages", "stream", "max_tokens"} & value.keys():
        raise ValueError("Extra body cannot override model, messages, stream, or max_tokens")
    return value


def load_protocol():
    path = Path(__file__).with_name("protocol.py")
    spec = importlib.util.spec_from_file_location("clashbench_api_protocol", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def iter_samples(path, protocol, limit):
    seen = set()
    count = 0
    with Path(path).open() as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
                samples = protocol.iter_samples(case)
                for sample in samples:
                    if limit is not None and count >= limit:
                        return
                    sample_id = sample["sample_id"]
                    if sample_id in seen:
                        raise ValueError("Duplicate sample_id in input")
                    seen.add(sample_id)
                    count += 1
                    yield sample
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid evaluation case at line {line_number}: {type(exc).__name__}") from exc


def completed_ids(path, model):
    done = set()
    if not Path(path).exists():
        return done
    with Path(path).open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (row.get("requested_model") == model and row.get("status") == "predicted"
                    and row.get("decision") in VALID_DECISIONS and row.get("sample_id")):
                done.add(row["sample_id"])
    return done


def add_usage(total, usage):
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, dict):
            nested = total.setdefault(key, {})
            if isinstance(nested, dict):
                add_usage(nested, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(total.get(key, 0), (int, float)):
                total[key] = total.get(key, 0) + value


def atomic_progress(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def request_once(sample, args, credentials, extra_body, parse_decision, attempt, run_id):
    row = {
        "id": sample["sample_id"], "sample_id": sample["sample_id"],
        "trace_id": sample["trace_id"], "case": sample["case"], "mode": sample["mode"],
        "label": sample["label"], "selected_step": sample.get("selected_step"),
        "reference": {"label": sample["label"], "selected_step": sample.get("selected_step")},
        "requested_model": args.model, "returned_model": None,
        "response_id": None, "system_fingerprint": None,
        "content": None, "reasoning": None, "finish_reason": None, "usage": {},
        "decision": None, "reason": None, "status": "request_error",
        "attempt": attempt, "run_id": run_id, "created_at": utc_now(),
    }
    body = {
        "model": args.model, "messages": sample["messages"], "temperature": 0,
        "max_tokens": args.max_tokens, "stream": False,
    }
    body.update(extra_body)
    request = Request(
        credentials["endpoint"], data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + credentials["api_key"]},
        method="POST",
    )
    started = time.monotonic()
    retryable = True
    try:
        open_request = build_opener(ProxyHandler({})).open if args.no_proxy else urlopen
        with open_request(request, timeout=args.timeout) as response:
            row["http_status"] = response.status
            data = json.loads(response.read())
        row["returned_model"] = data.get("model")
        row["response_id"] = data.get("id")
        row["system_fingerprint"] = data.get("system_fingerprint")
        row["usage"] = data.get("usage") or {}
        choice = data["choices"][0]
        message = choice["message"]
        row["content"] = message.get("content")
        row["reasoning"] = message.get("reasoning_content", message.get("reasoning"))
        row["finish_reason"] = choice.get("finish_reason")
        if row["finish_reason"] == "length":
            row["status"] = "output_truncated"
        elif row["finish_reason"] == "content_filter":
            row["status"] = "output_filtered"
        else:
            try:
                decision, reason = parse_decision(row["content"])
                if decision not in VALID_DECISIONS | {"unknown"}:
                    raise ValueError("Unsupported parsed decision")
                row.update(decision=decision, reason=reason,
                           status="predicted" if decision in VALID_DECISIONS else "unknown")
            except (ValueError, TypeError, KeyError) as exc:
                row.update(status="parse_error", error=sanitize(str(exc), credentials["api_key"])[:2000])
    except HTTPError as exc:
        row["http_status"] = exc.code
        try:
            text = exc.read(65536 + len(credentials["api_key"])).decode("utf-8", errors="replace")
        except (HTTPException, URLError, TimeoutError, OSError) as body_exc:
            row["error"] = sanitize(f"Incomplete HTTP error response: {body_exc}", credentials["api_key"])[:2000]
        else:
            row["error"] = sanitize(text, credentials["api_key"])[:2000]
            overflow = any(term in text.lower() for term in (
                "context_length", "maximum context", "context window", "too many tokens", "input too long",
            ))
            if overflow:
                row["status"] = "context_overflow"
                retryable = False
            else:
                retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
    except (HTTPException, URLError, TimeoutError, OSError) as exc:
        row["error"] = sanitize(str(exc), credentials["api_key"])[:2000]
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        row.update(status="parse_error", error=f"Invalid API response: {type(exc).__name__}")
    finally:
        row["latency_seconds"] = time.monotonic() - started
    return sanitize(row, credentials["api_key"]), retryable


def execute_sample(sample, args, credentials, extra_body, protocol, emit, stopped, run_id):
    last = None
    for attempt in range(1, args.attempts + 1):
        if stopped.is_set():
            break
        last, retryable = request_once(
            sample, args, credentials, extra_body, protocol.parse_decision, attempt, run_id,
        )
        emit(last)
        if last["status"] in {"predicted", "unknown"} or not retryable or attempt == args.attempts:
            break
        if stopped.wait(min(RETRY_BASE_SECONDS * 2 ** (attempt - 1), 8.0)):
            break
    return last


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", required=True, help="Exact model identifier sent to the API")
    result.add_argument("--credentials", required=True, help="Private external JSON with base_url and api_key")
    result.add_argument("--data", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--progress", required=True)
    result.add_argument("--concurrency", type=int, default=16)
    result.add_argument("--max-tokens", type=int, default=256)
    result.add_argument("--timeout", type=float, default=120)
    result.add_argument("--attempts", type=int, default=3)
    result.add_argument("--limit", type=int, help="Process the first N stable samples, including resume skips")
    result.add_argument("--extra-body", help="JSON request options, such as disabling reasoning")
    result.add_argument("--no-proxy", action="store_true", help="Bypass configured HTTP(S) proxies for API requests")
    return result


def main(argv=None, *, protocol_module=None):
    args = parser().parse_args(argv)
    if min(args.concurrency, args.max_tokens, args.timeout, args.attempts) <= 0 or (args.limit is not None and args.limit < 0):
        raise SystemExit("concurrency, max-tokens, timeout and attempts must be positive; limit must be nonnegative")
    credentials = None
    stopped = threading.Event()
    signal_received = []
    previous_handlers = {}
    progress = {
        "state": "starting", "model": args.model, "started_at": utc_now(),
        "samples_seen": 0, "samples_submitted": 0, "samples_completed": 0,
        "samples_skipped_resume": 0, "samples_failed": 0, "samples_abstained": 0,
        "samples_interrupted": 0,
        "attempts": 0, "status_counts": {}, "usage": {}, "enumeration_complete": False,
        "concurrency": args.concurrency, "max_tokens": args.max_tokens,
        "timeout_seconds": args.timeout, "attempt_limit": args.attempts,
        "sample_limit": args.limit, "pid": os.getpid(),
        "no_proxy": args.no_proxy,
    }
    lock = threading.Lock()
    output = None
    executor = None
    run_id = str(uuid.uuid4())
    progress["run_id"] = run_id

    def snapshot():
        with lock:
            progress["updated_at"] = utc_now()
            value = json.loads(json.dumps(progress))
        atomic_progress(args.progress, value)

    def on_signal(number, frame):
        signal_received.append(number)
        stopped.set()

    def emit(row):
        with lock:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
            output.flush()
            progress["attempts"] += 1
            statuses = progress["status_counts"]
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
            add_usage(progress["usage"], row.get("usage"))

    try:
        credentials = load_credentials(args.credentials)
        extra_body = load_extra_body(args.extra_body)
        protocol = protocol_module or load_protocol()
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[number] = signal.signal(number, on_signal)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = output_path.open("a+", encoding="utf-8")
        if os.name == "posix":
            import fcntl
            fcntl.flock(output.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        done = completed_ids(output_path, args.model)
        output.seek(0, os.SEEK_END)
        if output.tell():
            with output_path.open("rb") as last_byte:
                last_byte.seek(-1, os.SEEK_END)
                if last_byte.read(1) != b"\n":
                    output.write("\n")
                    output.flush()
        samples = iter_samples(args.data, protocol, args.limit)
        executor = ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="clashbench-api")
        pending = {}
        exhausted = False
        progress["state"] = "running"
        snapshot()
        last_progress = time.monotonic()

        def fill():
            nonlocal exhausted
            while not stopped.is_set() and not exhausted and len(pending) < args.concurrency:
                try:
                    sample = next(samples)
                except StopIteration:
                    exhausted = True
                    progress["enumeration_complete"] = True
                    break
                progress["samples_seen"] += 1
                if sample["sample_id"] in done:
                    progress["samples_skipped_resume"] += 1
                    progress["samples_completed"] += 1
                    continue
                future = executor.submit(
                    execute_sample, sample, args, credentials, extra_body, protocol, emit, stopped, run_id,
                )
                pending[future] = sample["sample_id"]
                progress["samples_submitted"] += 1

        fill()
        while pending:
            finished, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in finished:
                pending.pop(future)
                row = future.result()
                if row and row["status"] == "predicted":
                    progress["samples_completed"] += 1
                elif row and row["status"] == "unknown":
                    progress["samples_abstained"] += 1
                elif row:
                    progress["samples_failed"] += 1
                else:
                    progress["samples_interrupted"] += 1
            fill()
            if time.monotonic() - last_progress >= PROGRESS_INTERVAL_SECONDS:
                snapshot()
                last_progress = time.monotonic()
        progress["state"] = "interrupted" if stopped.is_set() else (
            "finished_with_errors" if progress["samples_failed"] else
            "finished_with_abstentions" if progress["samples_abstained"] else "finished"
        )
        progress["expected_samples"] = progress["samples_seen"] if exhausted else None
        snapshot()
        if signal_received:
            return 128 + signal_received[0]
        return 1 if progress["samples_failed"] or progress["samples_abstained"] else 0
    except Exception as exc:
        stopped.set()
        progress["state"] = "failed"
        key = credentials["api_key"] if credentials else ""
        # Configuration parsing errors expose only their type, never JSON source.
        progress["error"] = sanitize(f"Runner failure: {type(exc).__name__}", key)
        snapshot()
        print(progress["error"], file=sys.stderr)
        return 2
    finally:
        stopped.set()
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
        if output:
            output.close()
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
