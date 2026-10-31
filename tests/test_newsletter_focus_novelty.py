from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from opportunity_radar.conditions import match_job_conditions
from opportunity_radar.filtering import dedupe_jobs, remove_seen_jobs, source_filter_allows
from opportunity_radar.models import JobPosting, RecipientResult
from opportunity_radar.pipeline import ranked_from_evaluated_state, run_digest
from opportunity_radar.ranker import rank_deterministically
from opportunity_radar.state import FileJsonStore, GithubJsonStore, merge_state
from tests.test_state import FakeResponse

ROOT = Path(__file__).resolve().parents[1]
ENV = {"OPPORTUNITY_RECIPIENTS": "one@example.com,two@example.com", "OPPORTUNITY_SEND_FREQUENCY": "daily", "OPPORTUNITY_MAX_JOBS": "1"}


def job(title="Strategy Associate", description="", **kwargs):
    payload = dict(source_id="board", source_name="Board", external_id="123", title=title, company="Example", city="New York", location_text="New York", canonical_url="https://boards.greenhouse.io/acme/jobs/123?utm_source=digest", description_text=description)
    return JobPosting(**{**payload, **kwargs})


def evaluated(*jobs):
    return {"evaluated_jobs": {item.stable_key: {"status": "included", "score": 95 - index, "job": item.to_dict()} for index, item in enumerate(jobs)}}


class RoleFocusTests(unittest.TestCase):
    def test_focused_roles_pass_even_with_legacy_broad_conditions(self):
        allowed = [
            ("GTM Strategy Associate", ""), ("Go-to-Market Manager", ""),
            ("Revenue Operations Analyst", ""), ("Business Operations Associate", ""),
            ("Strategy and Operations Analyst", ""), ("Strategy Associate", ""),
            ("Deployment Strategist", ""), ("Forward Deployed Strategist", ""),
            ("Chief of Staff, AI Strategy", ""),
            ("Product Manager", "3 years in product management or management consulting."),
            ("Product Manager", "We welcome candidates from consulting backgrounds."),
        ]
        for title, description in allowed:
            with self.subTest(title=title, description=description):
                item = job(title, description)
                self.assertTrue(source_filter_allows(item, {}))
                self.assertTrue(match_job_conditions(item, {}).allowed)

    def test_broad_keywords_and_pm_without_consulting_alternative_cannot_pass(self):
        excluded = [
            ("Public Policy Associate", ""), ("Venture Analyst", ""),
            ("Operations Associate", ""), ("Retail Operations Manager", ""),
            ("GTM Software Engineer", "Work on go-to-market strategy."),
            ("Deployment Engineer", ""), ("Associate Consultant", ""),
            ("Strategy Consultant", ""), ("Product Manager Intern", "Consulting welcomed."),
            ("Brand Designer", "We are a company with a global GTM strategy."),
            ("Product Manager", "Product management experience required. Partner with consultants."),
            ("Product Manager", "Consulting experience is not accepted; 3 years of product management required."),
            ("Product Manager", "3 years of product management required. Consulting experience is a plus."),
            ("Product Manager", ""),
        ]
        for title, description in excluded:
            with self.subTest(title=title, description=description):
                item = job(title, description)
                legacy = {"role_description_chars": 8000, "role_groups": [{"id": "everything", "include_any": ["policy", "venture", "operations", "strategy", "product", "gtm", "deployment", "consultant", "designer"]}]}
                self.assertFalse(source_filter_allows(item, {}))
                self.assertFalse(match_job_conditions(item, legacy).allowed)

    def test_ranking_and_legacy_evaluated_records_cannot_bypass_focus(self):
        item = job("AI Policy Associate", "OpenAI global AI policy strategy startup.")
        self.assertEqual(rank_deterministically([item], max_selected=5), [])
        selected, count = ranked_from_evaluated_state(evaluated(item), max_jobs=5, max_jobs_per_company=2)
        self.assertEqual(selected, [])
        self.assertEqual(count, 0)

    def test_focused_gtm_and_deployment_roles_have_fallback_signal(self):
        for title in ("GTM Associate", "Deployment Strategist", "Forward Deployed Strategist", "BizOps Associate"):
            with self.subTest(title=title):
                self.assertEqual(len(rank_deterministically([job(title)], max_selected=1)), 1)


class NoveltyTests(unittest.TestCase):
    def test_alias_tracking_urls_dedupe_without_collapsing_distinct_requisitions(self):
        original = job()
        alias = job(source_id="registry-greenhouse-acme", canonical_url="https://job-boards.greenhouse.io/acme/jobs/123/#application")
        other = job(external_id="456", canonical_url="https://boards.greenhouse.io/acme/jobs/456")
        self.assertEqual(len(dedupe_jobs([original, alias, other])), 2)
        state = {"sent_jobs": {original.stable_key: {"url": original.canonical_url}}}
        self.assertEqual(remove_seen_jobs([alias, other], state), [other])

    def test_only_delivered_legacy_seen_entries_count_and_url_only_ids_match(self):
        original = job()
        alias = job(source_id="", external_id="", canonical_url="https://job-boards.greenhouse.io/acme/jobs/123?gh_src=abc")
        state = {"seen_jobs": {original.stable_key: {"first_seen_at": "today", "url": original.canonical_url}}}
        self.assertEqual(remove_seen_jobs([alias], state), [alias])
        state["seen_jobs"][original.stable_key]["first_sent_at"] = "today"
        self.assertEqual(remove_seen_jobs([alias], state), [])

    def test_consecutive_daily_sends_and_force_never_resend_url_aliases(self):
        original = job()
        alias = job(source_id="registry", canonical_url="https://job-boards.greenhouse.io/acme/jobs/123?ref=mail")
        other = job(external_id="456", canonical_url="https://example.com/jobs/456", company="Other")
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save(evaluated(original, alias, other))
            sender = RecordingSender(store)
            first = run_digest(ROOT, send=True, from_state=True, state_store=store, sender=sender, now=datetime.fromisoformat("2026-10-26T12:00:00+00:00"))
            second = run_digest(ROOT, send=True, force=True, include_seen=True, from_state=True, state_store=store, sender=sender, now=datetime.fromisoformat("2026-10-27T12:00:00+00:00"))
            self.assertEqual(first.selected_jobs[0].job.external_id, "123")
            self.assertEqual(second.selected_jobs[0].job.external_id, "456")
            self.assertTrue(sender.persisted_before_send)

    def test_partial_or_uncertain_delivery_is_held_and_preview_does_not_consume(self):
        for mode in ("partial", "exception", "failed"):
            with self.subTest(mode=mode), patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                store.save(evaluated(job()))
                initial = store.path.read_bytes()
                run_digest(ROOT, from_state=True, state_store=store)
                self.assertEqual(store.path.read_bytes(), initial)
                sender = RecordingSender(store, mode)
                result = run_digest(ROOT, send=True, from_state=True, state_store=store, sender=sender)
                self.assertTrue(result.state_summary["mutated"])
                self.assertFalse(store.load().get("sent_jobs"))
                self.assertTrue(store.load().get("delivery_attempts"))
                retry = RecordingSender(store)
                repeated = run_digest(ROOT, send=True, force=True, include_seen=True, from_state=True, state_store=store, sender=retry)
                self.assertEqual(repeated.selected_jobs, [])
                self.assertEqual(retry.calls, [])
                self.assertTrue(sender.persisted_before_send)

    def test_journal_failure_stops_before_external_send(self):
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save(evaluated(job()))
            sender = RecordingSender(store)
            with patch.object(store, "claim_delivery", side_effect=OSError("disk full")):
                result = run_digest(ROOT, send=True, from_state=True, state_store=store, sender=sender)
            self.assertEqual(sender.calls, [])
            self.assertTrue(any(error.startswith("delivery_journal_failed") for error in result.errors))

    def test_stale_discovery_save_preserves_delivery_state_on_first_put(self):
        store = GithubJsonStore("owner/repo", "state.json", "token", user_agent="test")
        current = {"sent_jobs": {"delivered": {"url": "https://example.com/1"}}, "delivery_attempts": {"held": {"status": "pending"}}}
        with patch.object(store, "load_with_sha", return_value=(current, "new-sha")), patch("urllib.request.urlopen", return_value=FakeResponse("{}")) as opened:
            store.save({"evaluated_jobs": {"new": {"status": "included"}}})
        body = json.loads(opened.call_args.args[0].data)
        saved = json.loads(base64.b64decode(body["content"]))
        self.assertEqual(saved.get("sent_jobs"), current["sent_jobs"])
        self.assertEqual(saved.get("delivery_attempts"), current["delivery_attempts"])

    def test_merge_preserves_attempts_and_holds(self):
        self.assertEqual(merge_state({"delivery_attempts": {"old": {}}}, {"delivery_attempts": {"new": {}}})["delivery_attempts"], {"old": {}, "new": {}})


class RecordingSender:
    def __init__(self, store, mode="success"):
        self.store = store
        self.mode = mode
        self.calls = []
        self.persisted_before_send = True

    def send(self, recipient, message):
        self.calls.append(recipient)
        self.persisted_before_send &= bool(self.store.load().get("delivery_attempts"))
        if self.mode == "exception":
            raise TimeoutError("response lost")
        ok = self.mode != "failed" and not (self.mode == "partial" and len(self.calls) == 2)
        return RecipientResult(recipient=recipient, ok=ok, provider="fake", error="uncertain" if not ok else "")
