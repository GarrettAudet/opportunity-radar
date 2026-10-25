from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_radar.pipeline import run_digest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the weekly OpportunityRadar digest.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--send", action="store_true", help="Send the digest to configured recipients")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending; this is the default")
    parser.add_argument("--force", action="store_true", help="Ignore sent-week and seen-job suppression")
    parser.add_argument("--respect-schedule", action="store_true", help="Only send on the configured local day, at or after the send hour")
    parser.add_argument("--sources", default="", help="Optional sources JSON path")
    parser.add_argument("--deterministic-fallback", action="store_true", help="Use deterministic ranking if OpenRouter is unavailable")
    parser.add_argument("--include-seen", action="store_true", help="Include jobs already seen in state")
    parser.add_argument("--from-state", action="store_true", help="Build the weekly digest from daily evaluated jobs in state")
    parser.add_argument("--json", action="store_true", help="Print full JSON")
    args = parser.parse_args()

    result = run_digest(
        Path(args.root),
        send=bool(args.send),
        force=bool(args.force),
        respect_schedule=bool(args.respect_schedule),
        sources_path=args.sources,
        deterministic_fallback=True if args.deterministic_fallback else None,
        include_seen=bool(args.include_seen),
        from_state=bool(args.from_state),
    )
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(result.digest_text)
        print()
        print(f"Candidates: {result.candidate_count}")
        print(f"Selected: {len(result.selected_jobs)}")
        print(f"Errors: {', '.join(result.errors) if result.errors else 'none'}")
        if result.recipient_results:
            ok = sum(1 for item in result.recipient_results if item.ok)
            print(f"Recipients sent: {ok}/{len(result.recipient_results)}")
    if any(error.startswith("ranker_failed") for error in result.errors):
        return 1
    if result.send_requested and result.recipient_results and not all(item.ok for item in result.recipient_results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
