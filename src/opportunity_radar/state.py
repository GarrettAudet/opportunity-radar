from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
import urllib.error
import urllib.request

from .models import now_iso
from .file_lock import exclusive_file_lock
from .filtering import delivered_job_identities


MAX_STORED_RUNS = 50


class JsonStore(Protocol):
    def load(self) -> dict[str, Any]:
        ...

    def save(self, payload: dict[str, Any]) -> None:
        ...

    def claim_delivery(self, payload: dict[str, Any], run_id: str, attempt: dict[str, Any]) -> dict[str, Any] | None:
        """Atomically persist a new one-use claim, or deny an existing identity."""
        ...


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "seen_jobs": {},
        "sent_jobs": {},
        "sent_weeks": {},
        "sent_days": {},
        "delivery_attempts": {},
        "evaluated_jobs": {},
        "source_cache": {},
        "board_registry": {},
        "runs": [],
    }


class FileJsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def load(self) -> dict[str, Any]:
        with exclusive_file_lock(self.lock_path):
            return self._read() if self.path.exists() else empty_state()

    def _replace(self, payload: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save(self, payload: dict[str, Any]) -> None:
        with exclusive_file_lock(self.lock_path):
            current = self._read()
            state_keys = {"seen_jobs", "sent_jobs", "sent_days", "sent_weeks", "delivery_attempts", "evaluated_jobs", "board_registry", "source_cache", "runs"}
            # Config-only callers keep replacement semantics; state snapshots
            # must retain durable delivery history across stale saves.
            merged = merge_state(current, payload) if state_keys & (current.keys() | payload.keys()) else payload
            self._replace(merged)

    def claim_delivery(self, payload: dict[str, Any], run_id: str, attempt: dict[str, Any]) -> dict[str, Any] | None:
        with exclusive_file_lock(self.lock_path):
            claimed = delivery_claim_payload(self._read(), payload, run_id, attempt)
            if claimed is not None:
                self._replace(claimed)
            return claimed


def delivery_claim_payload(current: dict[str, Any], incoming: dict[str, Any], run_id: str, attempt: dict[str, Any]) -> dict[str, Any] | None:
    """Called only inside a file lock or before a SHA-conditional GitHub PUT."""
    merged = merge_state(current, incoming)
    if not run_id or run_id in merged["delivery_attempts"]:
        return None
    identities: set[str] = set()
    for job in attempt.get("jobs", []):
        keys = job.get("identity_keys") if isinstance(job, dict) else None
        if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
            raise ValueError("invalid_delivery_claim_identity")
        identities.update(keys)
    if identities & delivered_job_identities(merged):
        return None
    merged["delivery_attempts"][run_id] = attempt
    return merged


class GithubJsonStore:
    def __init__(self, repo: str, path: str, token: str, ref: str = "main", *, user_agent: str) -> None:
        self.repo = repo
        self.path = path.strip("/")
        self.token = token
        self.ref = ref
        self.user_agent = user_agent

    def _request(self, url: str, *, method: str = "GET", data: bytes | None = None) -> urllib.request.Request:
        return urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )

    def _url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}/contents/{quote(self.path)}?ref={quote(self.ref)}"

    def _content_from_item(self, item: dict[str, Any]) -> str:
        content = str(item.get("content") or "")
        encoding = str(item.get("encoding") or "").lower()
        if content:
            if encoding == "base64":
                return base64.b64decode(content.encode("utf-8")).decode("utf-8-sig")
            return content

        download_url = str(item.get("download_url") or "").strip()
        download_error: urllib.error.HTTPError | None = None
        if download_url:
            try:
                with urllib.request.urlopen(self._request(download_url), timeout=30) as response:
                    return response.read().decode("utf-8-sig")
            except urllib.error.HTTPError as exc:
                download_error = exc

        sha = str(item.get("sha") or "").strip()
        if sha:
            blob_url = f"https://api.github.com/repos/{self.repo}/git/blobs/{quote(sha)}"
            with urllib.request.urlopen(self._request(blob_url), timeout=30) as response:
                blob = json.loads(response.read().decode("utf-8"))
            blob_content = str(blob.get("content") or "")
            if str(blob.get("encoding") or "").lower() == "base64":
                return base64.b64decode(blob_content.encode("utf-8")).decode("utf-8-sig")
            return blob_content

        if download_error is not None:
            raise download_error
        return ""

    def load_with_sha(self) -> tuple[dict[str, Any], str]:
        try:
            with urllib.request.urlopen(self._request(self._url()), timeout=30) as response:
                item = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return empty_state(), ""
            raise
        content = self._content_from_item(item)
        return json.loads(content), str(item.get("sha", ""))

    def load(self) -> dict[str, Any]:
        payload, _sha = self.load_with_sha()
        return payload

    def claim_delivery(self, payload: dict[str, Any], run_id: str, attempt: dict[str, Any]) -> dict[str, Any] | None:
        for retry in range(3):
            current, sha = self.load_with_sha()
            claimed = delivery_claim_payload(current, payload, run_id, attempt)
            if claimed is None:
                return None
            body = {
                "message": "Claim OpportunityRadar delivery",
                "content": base64.b64encode(json.dumps(claimed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii"),
                "branch": self.ref,
            }
            if sha:
                body["sha"] = sha
            put_url = f"https://api.github.com/repos/{self.repo}/contents/{quote(self.path)}"
            try:
                with urllib.request.urlopen(self._request(put_url, method="PUT", data=json.dumps(body).encode("utf-8")), timeout=30):
                    return claimed
            except urllib.error.HTTPError as exc:
                if exc.code in (409, 500, 502, 503, 504) and retry < 2:
                    if exc.code != 409:
                        time.sleep(2 ** retry)
                    # Rebuild the claim against current durable identities on
                    # every CAS retry. An ambiguous committed claim is denied
                    # on reload, including its original caller's attempt ID.
                    continue
                raise
        return None

    def save(self, payload: dict[str, Any]) -> None:
        write_payload = payload
        for attempt in range(3):
            current, sha = self.load_with_sha()
            # The caller may hold an old discovery snapshot. Adopting the latest
            # SHA without merging would overwrite intervening delivery receipts.
            write_payload = merge_state(current, write_payload)
            write_payload["updated_at"] = now_iso()
            body: dict[str, Any] = {
                "message": "Update OpportunityRadar state",
                # State can contain thousands of jobs and board results. Avoid
                # paying the upload-size cost of whitespace on every update.
                "content": base64.b64encode(json.dumps(write_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii"),
                "branch": self.ref,
            }
            if sha:
                body["sha"] = sha
            put_url = f"https://api.github.com/repos/{self.repo}/contents/{quote(self.path)}"
            try:
                with urllib.request.urlopen(
                    self._request(put_url, method="PUT", data=json.dumps(body).encode("utf-8")),
                    timeout=30,
                ):
                    return
            except urllib.error.HTTPError as exc:
                if exc.code == 409 and attempt < 2:
                    continue
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    # The write may have reached GitHub before the gateway
                    # failed. Reload its SHA and merge before retrying.
                    time.sleep(2 ** attempt)
                    continue
                # urllib's default exception hides GitHub's validation reason.
                # Report only error metadata, never the request or state data.
                annotate_github_error(exc, len(body["content"]), (self.token, self.repo, self.path))
                raise


def annotate_github_error(exc, encoded_bytes, sensitive_values):
    """Attach bounded provider metadata while redacting store identifiers."""
    try:
        error = json.loads(exc.read())
    except (ValueError, OSError):
        error = {}
    if isinstance(error, dict):
        detail = {
            "message": error.get("message", ""),
            "errors": error.get("errors", []),
            "upload_base64_bytes": encoded_bytes,
        }
        # GitHub can echo invalid values; do not log those.
        if isinstance(detail["errors"], list):
            detail["errors"] = [
                {key: item[key] for key in ("resource", "field", "code", "message") if key in item}
                for item in detail["errors"] if isinstance(item, dict)
            ]
        else:
            detail["errors"] = []
        summary = json.dumps(detail, ensure_ascii=True)
        for sensitive in sensitive_values:
            if sensitive:
                summary = summary.replace(sensitive, "[redacted]")
        exc.msg = f"{exc.reason}; GitHub validation: {summary[:1500]}"


def merge_state(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = empty_state()
    merged["seen_jobs"] = {**current.get("seen_jobs", {}), **incoming.get("seen_jobs", {})}
    merged["sent_jobs"] = {**current.get("sent_jobs", {}), **incoming.get("sent_jobs", {})}
    merged["sent_weeks"] = {**current.get("sent_weeks", {}), **incoming.get("sent_weeks", {})}
    merged["sent_days"] = {**current.get("sent_days", {}), **incoming.get("sent_days", {})}
    merged["delivery_attempts"] = {**current.get("delivery_attempts", {}), **incoming.get("delivery_attempts", {})}
    merged["evaluated_jobs"] = {**current.get("evaluated_jobs", {}), **incoming.get("evaluated_jobs", {})}
    merged["source_cache"] = {**current.get("source_cache", {}), **incoming.get("source_cache", {})}
    merged["board_registry"] = {**current.get("board_registry", {}), **incoming.get("board_registry", {})}
    runs = list(current.get("runs", [])) + list(incoming.get("runs", []))
    seen_run_ids = set()
    unique_runs = []
    for run in runs:
        run_id = str(run.get("run_id", ""))
        if run_id and run_id in seen_run_ids:
            continue
        if run_id:
            seen_run_ids.add(run_id)
        unique_runs.append(run)
    merged["runs"] = unique_runs[-MAX_STORED_RUNS:]
    merged["updated_at"] = now_iso()
    return merged


def state_store_from_env(root: Path) -> JsonStore:
    repo = os.environ.get("GITHUB_STATE_REPO", "").strip()
    token = os.environ.get("GITHUB_STATE_TOKEN", "").strip()
    if repo and token:
        return GithubJsonStore(
            repo,
            os.environ.get("GITHUB_STATE_PATH", "opportunity-state.json"),
            token,
            os.environ.get("GITHUB_STATE_REF", "main"),
            user_agent="opportunity-radar-state",
        )
    path = Path(os.environ.get("OPPORTUNITY_STATE_PATH", "data/state/opportunity-state.local.json"))
    if not path.is_absolute():
        path = root / path
    return FileJsonStore(path)


def load_json_from_github(repo: str, path: str, token: str, ref: str = "main") -> dict[str, Any]:
    encoded_path = quote(path.strip("/"))
    encoded_ref = quote(ref)
    url = f"https://api.github.com/repos/{repo}/contents/{encoded_path}?ref={encoded_ref}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.raw",
            "Authorization": f"Bearer {token}",
            "User-Agent": "opportunity-radar-config",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
