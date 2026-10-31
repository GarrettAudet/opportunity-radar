from __future__ import annotations

import base64
import copy
import io
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opportunity_radar.models import JobPosting, RankedOpportunity, RecipientResult
from opportunity_radar.pipeline import run_digest, send_selected_digest
from opportunity_radar.state import FileJsonStore, GithubJsonStore

ROOT = Path(__file__).resolve().parents[1]


def posting():
    return JobPosting(source_id="board", source_name="Board", external_id="123", title="Strategy Associate", company="Example", city="New York", location_text="New York", canonical_url="https://boards.greenhouse.io/acme/jobs/123")


def seed():
    item = posting()
    return {"evaluated_jobs": {item.stable_key: {"status": "included", "score": 95, "job": item.to_dict()}}}


class CaptureSender:
    def __init__(self):
        self.calls = []

    def send(self, recipient, message):
        self.calls.append(recipient)
        return RecipientResult(recipient, True, "offline")


def deliver(store, state, run_id, sender):
    errors = []
    results = send_selected_digest(config=SimpleNamespace(send_provider="offline", recipients=["test@example.com"], send_empty_digest=False), send=True, selected=[RankedOpportunity(posting(), 95, True, "fit", "cool")], digest_text="same posting", errors=errors, sender=sender, state=state, state_store=store, run_id=run_id)
    return results, errors


def process_delivery(path, state, run_id, start, output):
    try:
        start.wait(10)
        sender = CaptureSender()
        _, errors = deliver(FileJsonStore(Path(path)), state, run_id, sender)
        output.put({"calls": len(sender.calls), "errors": errors})
    except Exception as exc:
        output.put({"exception": type(exc).__name__ + ": " + str(exc)})


def process_save(path, payload, start, output):
    try:
        start.wait(10)
        FileJsonStore(Path(path)).save(payload)
        output.put({"ok": True})
    except Exception as exc:
        output.put({"exception": type(exc).__name__ + ": " + str(exc)})


class Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class GithubStateService:
    """Offline Contents API with a real revision/CAS check."""
    def __init__(self):
        self.state = seed()
        self.revision = 1
        self.puts = 0
        self.compete_on_put = False
        self.commit_then_gateway_error = False
        self.lock = threading.Lock()

    def __call__(self, request, timeout=30):
        with self.lock:
            if request.get_method() == "GET":
                return Response({"sha": str(self.revision), "encoding": "base64", "content": base64.b64encode(json.dumps(self.state).encode()).decode()})
            self.puts += 1
            payload = json.loads(request.data)
            if self.compete_on_put:
                self.compete_on_put = False
                self.state["delivery_attempts"] = {"competitor": {"status": "pending", "jobs": [{"identity_keys": [posting().stable_key]}]}}
                self.revision += 1
            if payload.get("sha") != str(self.revision):
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b"{}"))
            self.state = json.loads(base64.b64decode(payload["content"]))
            self.revision += 1
            if self.commit_then_gateway_error:
                self.commit_then_gateway_error = False
                raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"{}"))
            return Response({})


class DeliveryClaimTests(unittest.TestCase):
    def test_stale_file_discovery_cannot_erase_delivery_or_resend(self):
        env = {"OPPORTUNITY_RECIPIENTS": "test@example.com", "OPPORTUNITY_SEND_FREQUENCY": "daily"}
        with patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save(seed())
            stale = store.load()
            sender = CaptureSender()
            first = run_digest(ROOT, send=True, from_state=True, state_store=store, sender=sender)
            store.save(stale)
            second = run_digest(ROOT, send=True, from_state=True, state_store=store, sender=sender)
            self.assertEqual(len(sender.calls), 1)
            self.assertTrue(store.load().get("sent_jobs"))
            self.assertTrue(store.load().get("delivery_attempts"))

    def test_two_stale_github_snapshots_only_one_provider_call(self):
        service = GithubStateService()
        stores = [GithubJsonStore("offline/repo", "state.json", "dummy", user_agent="test") for _ in range(2)]
        sender = CaptureSender()
        with patch("urllib.request.urlopen", side_effect=service):
            stale = [store.load() for store in stores]
            deliver(stores[0], stale[0], "A", sender)
            _, errors = deliver(stores[1], stale[1], "B", sender)
        self.assertEqual(len(sender.calls), 1)
        self.assertIn("delivery_claim_conflict", errors)
        self.assertEqual(set(service.state["delivery_attempts"]), {"A"})

    def test_github_cas_conflict_rechecks_competing_hold_before_retry(self):
        service = GithubStateService()
        service.compete_on_put = True
        store = GithubJsonStore("offline/repo", "state.json", "dummy", user_agent="test")
        sender = CaptureSender()
        with patch("urllib.request.urlopen", side_effect=service):
            _, errors = deliver(store, seed(), "ours", sender)
        self.assertEqual(sender.calls, [])
        self.assertIn("delivery_claim_conflict", errors)
        self.assertEqual(service.puts, 1)

    def test_same_github_attempt_id_is_one_use_even_after_ambiguous_commit(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                service = GithubStateService()
                service.commit_then_gateway_error = ambiguous
                store = GithubJsonStore("offline/repo", "state.json", "dummy", user_agent="test")
                sender = CaptureSender()
                with patch("urllib.request.urlopen", side_effect=service), patch("opportunity_radar.state.time.sleep"):
                    deliver(store, seed(), "same-id", sender)
                    deliver(store, seed(), "same-id", sender)
                self.assertEqual(len(sender.calls), 0 if ambiguous else 1)
                self.assertIn("same-id", service.state["delivery_attempts"])

    def test_file_same_attempt_id_is_one_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save(seed())
            sender = CaptureSender()
            deliver(store, seed(), "same-id", sender)
            _, errors = deliver(store, seed(), "same-id", sender)
            self.assertEqual(len(sender.calls), 1)
            self.assertIn("delivery_claim_conflict", errors)

    def test_file_cross_process_claim_has_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            FileJsonStore(Path(path)).save(seed())
            context = multiprocessing.get_context("spawn")
            start, output = context.Event(), context.Queue()
            processes = [context.Process(target=process_delivery, args=(path, seed(), str(index), start, output)) for index in range(2)]
            for process in processes:
                process.start()
            start.set()
            messages = [output.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all("exception" not in result for result in messages), messages)
            self.assertEqual(sum(result["calls"] for result in messages), 1, messages)

    def test_file_cross_process_stale_saves_preserve_each_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            FileJsonStore(Path(path)).save(seed())
            context = multiprocessing.get_context("spawn")
            start, output = context.Event(), context.Queue()
            payloads = [{"evaluated_jobs": {}, "sent_jobs": {str(index): {"sent_at": "today"}}, "delivery_attempts": {str(index): {"status": "pending"}}} for index in range(2)]
            processes = [context.Process(target=process_save, args=(path, payload, start, output)) for payload in payloads]
            for process in processes:
                process.start()
            start.set()
            messages = [output.get(timeout=15) for _ in processes]
            for process in processes:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all("exception" not in result for result in messages), messages)
            state = FileJsonStore(Path(path)).load()
            self.assertEqual(set(state["sent_jobs"]), {"0", "1"})
            self.assertEqual(set(state["delivery_attempts"]), {"0", "1"})

    def test_file_atomic_replace_failure_leaves_previous_json_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save(seed())
            before = store.path.read_bytes()
            with patch("os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    store.save({"sent_jobs": {"new": {}}})
            self.assertEqual(store.path.read_bytes(), before)

    def test_unknown_store_without_atomic_claim_fails_closed(self):
        class Store:
            def save(self, payload):
                pass
        sender = CaptureSender()
        _, errors = deliver(Store(), seed(), "A", sender)
        self.assertEqual(sender.calls, [])
        self.assertIn("delivery_claim_unsupported", errors)

    def test_file_store_can_still_save_generic_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "config.json")
            store.save({"role_groups": [{"id": "one"}]})
            store.save({"role_groups": [{"id": "two"}]})
            self.assertEqual(store.load(), {"role_groups": [{"id": "two"}]})
