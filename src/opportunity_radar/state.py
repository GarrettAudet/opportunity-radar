from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
import urllib.error
import urllib.request

from .models import now_iso


MAX_STORED_RUNS = 50


class JsonStore(Protocol):
    def load(self) -> dict[str, Any]:
        ...

    def save(self, payload: dict[str, Any]) -> None:
        ...


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": now_iso(),
        "seen_jobs": {},
        "sent_jobs": {},
        "sent_weeks": {},
        "sent_days": {},
        "evaluated_jobs": {},
        "source_cache": {},
        "board_registry": {},
        "runs": [],
    }


class FileJsonStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_state()
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload["updated_at"] = now_iso()
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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

    def save(self, payload: dict[str, Any]) -> None:
        write_payload = payload
        for attempt in range(3):
            current, sha = self.load_with_sha()
            if attempt:
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
                try:
                    error = json.loads(exc.read())
                except (ValueError, OSError):
                    error = {}
                if isinstance(error, dict):
                    detail = {
                        "message": error.get("message", ""),
                        "errors": error.get("errors", []),
                        "upload_base64_bytes": len(body["content"]),
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
                    for sensitive in (self.token, self.repo, self.path):
                        if sensitive:
                            summary = summary.replace(sensitive, "[redacted]")
                    exc.msg = f"{exc.reason}; GitHub validation: {summary[:1500]}"
                raise


def merge_state(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = empty_state()
    merged["seen_jobs"] = {**current.get("seen_jobs", {}), **incoming.get("seen_jobs", {})}
    merged["sent_jobs"] = {**current.get("sent_jobs", {}), **incoming.get("sent_jobs", {})}
    merged["sent_weeks"] = {**current.get("sent_weeks", {}), **incoming.get("sent_weeks", {})}
    merged["sent_days"] = {**current.get("sent_days", {}), **incoming.get("sent_days", {})}
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
