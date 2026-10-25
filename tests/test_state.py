from __future__ import annotations

import base64
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from opportunity_radar.state import GithubJsonStore


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class GithubJsonStoreTests(unittest.TestCase):
    def test_save_retries_preserve_daily_delivery_markers(self) -> None:
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        current = {"sent_days": {"2026-10-26": {"run_id": "other"}}}
        incoming = {"sent_days": {"2026-10-27": {"run_id": "ours"}}}
        conflict = urllib.error.HTTPError("https://api.github.com", 409, "Conflict", {}, io.BytesIO(b"{}"))
        with patch.object(store, "load_with_sha", side_effect=[({}, "old"), (current, "new")]), patch("urllib.request.urlopen", side_effect=[conflict, FakeResponse("{}")]) as urlopen:
            store.save(incoming)
        body = json.loads(urlopen.call_args.args[0].data)
        saved = json.loads(base64.b64decode(body["content"]))
        self.assertEqual(saved.get("sent_days"), {**current["sent_days"], **incoming["sent_days"]})

    def test_save_compacts_upload_without_losing_state_or_unicode(self) -> None:
        payload = {"version": 1, "evaluated_jobs": {"job-1": {"title": "北京 analyst", "description": "two  spaces\nnew line"}}, "sent_weeks": {"2026-W40": {"run_id": "sent"}}}
        store = GithubJsonStore("owner/repo", "opportunity-state.json", "token", user_agent="test")
        requests = []

        def fake_urlopen(request, timeout=30):
            requests.append(request)
            return FakeResponse("{}")

        with patch.object(store, "load_with_sha", return_value=({}, "old-sha")), patch("urllib.request.urlopen", fake_urlopen):
            store.save(payload)

        body = json.loads(requests[0].data)
        uploaded = base64.b64decode(body["content"]).decode("utf-8")
        self.assertEqual(json.loads(uploaded), payload)
        self.assertLess(len(uploaded), len(json.dumps(payload, ensure_ascii=False, indent=2)))
        self.assertEqual(body["sha"], "old-sha")
        self.assertEqual(body["branch"], "main")
        self.assertEqual(requests[0].get_method(), "PUT")

    def test_save_reports_validation_reason_without_credentials_or_values(self) -> None:
        store = GithubJsonStore("owner/private", "state.json", "secret-token", user_agent="test")
        error = urllib.error.HTTPError("https://api.github.com", 422, "Unprocessable Entity", {}, io.BytesIO(json.dumps({
            "message": "Invalid request for owner/private/state.json secret-token",
            "errors": [{"resource": "Commit", "field": "content", "code": "too_large", "value": "private-payload"}],
        }).encode("utf-8")))
        with patch.object(store, "load_with_sha", return_value=({}, "sha")), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                store.save({"seen_jobs": {}})
        message = str(caught.exception)
        self.assertIn("too_large", message)
        self.assertIn("422", message)
        for sensitive in ("owner/private", "state.json", "secret-token", "private-payload"):
            self.assertNotIn(sensitive, message)

    def test_save_preserves_http_error_when_response_is_not_json(self) -> None:
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        error = urllib.error.HTTPError("https://api.github.com", 422, "Unprocessable Entity", {}, io.BytesIO(b"not JSON"))
        with patch.object(store, "load_with_sha", return_value=({}, "sha")), patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                store.save({})
        self.assertEqual(caught.exception.code, 422)

    def test_save_retries_conflict_with_latest_sha_and_merged_state(self) -> None:
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        requests = []

        def fake_urlopen(request, timeout=30):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(b"{}"))
            return FakeResponse("{}")

        with patch.object(store, "load_with_sha", side_effect=[({}, "old"), ({"sent_jobs": {"other": {"sent": True}}}, "new")]), patch("urllib.request.urlopen", fake_urlopen):
            store.save({"sent_jobs": {"ours": {"sent": True}}})
        self.assertEqual([request["sha"] for request in requests], ["old", "new"])
        saved = json.loads(base64.b64decode(requests[1]["content"]))
        self.assertEqual(set(saved["sent_jobs"]), {"ours", "other"})

    def test_save_recovers_from_gateway_error_using_latest_sha(self) -> None:
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        error = urllib.error.HTTPError("https://api.github.com", 502, "Bad Gateway", {}, io.BytesIO(b"{}"))
        with patch.object(store, "load_with_sha", side_effect=[({}, "old"), ({"sent_jobs": {"other": {}}}, "new")]), patch("urllib.request.urlopen", side_effect=[error, FakeResponse("{}")]) as urlopen, patch("opportunity_radar.state.time.sleep") as sleep:
            store.save({"sent_jobs": {"ours": {}}})
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["sha"], "new")
        self.assertEqual(set(json.loads(base64.b64decode(body["content"]))["sent_jobs"]), {"other", "ours"})
        sleep.assert_called_once_with(1)

    def test_save_stops_after_three_gateway_failures(self) -> None:
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        errors = [urllib.error.HTTPError("https://api.github.com", 502, "Bad Gateway", {}, io.BytesIO(b'{"message":"Server Error"}')) for _ in range(3)]
        with patch.object(store, "load_with_sha", return_value=({}, "sha")), patch("urllib.request.urlopen", side_effect=errors) as urlopen, patch("opportunity_radar.state.time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                store.save({})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("Server Error", str(caught.exception))

    def test_load_with_sha_decodes_small_contents_api_file(self) -> None:
        payload = {"version": 1, "seen_jobs": {}}
        encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        item = {"content": encoded, "encoding": "base64", "sha": "abc123"}

        def fake_urlopen(request, timeout=30):
            return FakeResponse(json.dumps(item))

        store = GithubJsonStore("owner/repo", "opportunity-state.json", "token", user_agent="test")
        with patch("urllib.request.urlopen", fake_urlopen):
            loaded, sha = store.load_with_sha()

        self.assertEqual(loaded, payload)
        self.assertEqual(sha, "abc123")

    def test_load_with_sha_uses_download_url_when_contents_api_omits_content(self) -> None:
        payload = {"version": 1, "board_registry": {"greenhouse:coolco": {}}}
        calls: list[str] = []
        item = {
            "content": "",
            "encoding": "none",
            "sha": "large123",
            "download_url": "https://raw.githubusercontent.com/owner/repo/main/opportunity-state.json",
        }

        def fake_urlopen(request, timeout=30):
            calls.append(request.full_url)
            if "api.github.com/repos/owner/repo/contents" in request.full_url:
                return FakeResponse(json.dumps(item))
            if "raw.githubusercontent.com/owner/repo" in request.full_url:
                return FakeResponse(json.dumps(payload))
            raise AssertionError(f"Unexpected URL: {request.full_url}")

        store = GithubJsonStore("owner/repo", "opportunity-state.json", "token", user_agent="test")
        with patch("urllib.request.urlopen", fake_urlopen):
            loaded, sha = store.load_with_sha()

        self.assertEqual(loaded, payload)
        self.assertEqual(sha, "large123")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
