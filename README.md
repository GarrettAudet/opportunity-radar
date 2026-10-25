# Job Discovery Newsletter

Job Discovery Newsletter turns scattered job postings into a shortlist tailored to a defined audience. It combines public job-board discovery, configurable filters, and AI-assisted ranking against editable criteria. Originally designed for Schwarzman Scholars, the workflow separates discovery from delivery, tracks previously evaluated opportunities, and supports digest previews before sending.

The package, workflow, and configuration names use OpportunityRadar. The intended deployment described here uses GitHub Actions for discovery and digest jobs, a private repository for durable JSON state, Gmail SMTP for delivery, and Google Groups to manage the audience.

## Current Configuration

The repository includes discovery and ranking code, multiple delivery providers, fixture-backed tests, and local preview scripts. The Gmail SMTP and Google Groups setup below describes the intended deployment; the checked-in defaults differ from it.

| Setting | Checked-in application and workflow defaults | Gmail setup described below |
| --- | --- | --- |
| Delivery provider | `twilio_whatsapp` | `gmail_smtp` |
| Time zone | `America/Edmonton` | `Asia/Shanghai` |
| Send day | Monday | Wednesday |
| Send hour | 09:00 local time | 09:00 local time |

The [workflow](.github/workflows/opportunity-radar-schedule.yml) currently triggers its weekly send guard at minute 17 of each hour on Mondays UTC (`17 * * * MON`). Changing the runtime day to Wednesday alone will not create a Wednesday workflow trigger. Before enabling scheduled delivery, set the provider explicitly and align the weekly cron, the workflow's schedule comparisons, and the runtime day, hour, and time zone.

[Application defaults](src/opportunity_radar/config.py), [Render configuration](render.yaml), and [cost-control guidance](docs/cost-controls.md) retain Twilio-oriented settings or instructions. Review the provider-specific requirements before using them. These checked-in files describe available behavior; they do not verify the live deployment's settings or successful delivery.

## What It Does

- Discovers public Greenhouse boards from Common Crawl and stores reusable board tokens in state.
- Polls active ATS boards daily, normalizes target-city postings, and filters out noisy roles before ranking.
- Uses `docs/opportunity-criteria.md` plus an LLM ranker to decide which roles belong in the digest.
- Stores evaluated jobs in durable JSON state so the weekly digest can send only unsent included opportunities.
- Sends one weekly plain-text email through Gmail SMTP to a configured Google Group.
- Leaves subscriber management, additions, removals, and delivery permissions to Google Groups.
- Provides local scripts, fixture-backed smoke runs, and protected API endpoints for manual checks.

## How It Runs

The app has three scheduled jobs in `.github/workflows/opportunity-radar-schedule.yml`:

| Job | Purpose | Sends email |
| --- | --- | --- |
| Registry refresh | Finds and stores public ATS board tokens. | No |
| Daily discovery | Polls boards, filters/ranks jobs, and writes evaluated state. | No |
| Weekly digest | Reads unsent included jobs from state and emails the Google Group. | Yes, when configured |

Manual workflow tasks behave differently:

| Task | What it does |
| --- | --- |
| `all-preview` | Runs registry, discovery, and a digest preview. It never sends email. |
| `registry` | Runs only the registry refresh. |
| `discovery` | Runs only daily discovery. |
| `weekly-preview` | Builds the weekly digest from state without sending. |
| `weekly-send` | Checks Gmail SMTP readiness, then sends the digest from state. |

A scheduled weekly run sends only when `OPPORTUNITY_SCHEDULER_ENABLED=true` and it is the configured `OPPORTUNITY_SEND_DOW` in `OPPORTUNITY_TIMEZONE`, at or after `OPPORTUNITY_SEND_HOUR`. The window stays open until local midnight so delayed runs can catch up. The saved `sent_weeks` entry prevents later runs from sending again that week. Manual `weekly-send` is for test sends and operational retries.

## Architecture

```mermaid
flowchart LR
    A[Weekly/Manual Registry Refresh] --> B[Common Crawl Board Registry]
    B --> C[Private JSON State]
    D[Daily GitHub Actions Scheduler] --> E[Poll Active Greenhouse Boards]
    C <--> E
    E --> F[City + Condition Filters]
    F --> G[LLM Ranker]
    G --> H[Evaluated Jobs in State]
    H <--> C
    I[Weekly GitHub Actions Scheduler] --> J[Digest From Unsent Winners]
    J <--> C
    J --> K[Gmail SMTP Sender]
    L[Protected API] --> J
    M[Criteria Markdown] --> G
```

## Gmail And Google Groups Setup

Production delivery assumes:

- A Google Group exists for recipients, for example `schwarzman-job-updates@googlegroups.com`.
- The Gmail account used as sender has SMTP access and an app password.
- The Google Group allows the sender address to post to the group.
- GitHub Actions has the repo secrets and variables below.

Use GitHub repository secrets for sensitive values:

```text
OPPORTUNITY_STATE_REPO=<owner/private-state-repo>
OPPORTUNITY_STATE_TOKEN=<fine-grained contents read/write token>
OPPORTUNITY_RECIPIENTS=schwarzman-job-updates@googlegroups.com
OPENROUTER_API_KEY=<capped OpenRouter key>
SMTP_USERNAME=schwarzmanjobupdates@gmail.com
SMTP_APP_PASSWORD=<Gmail app password>
```

Use GitHub repository variables for non-secret settings:

```text
OPPORTUNITY_SCHEDULER_ENABLED=true
OPPORTUNITY_TIMEZONE=Asia/Shanghai
OPPORTUNITY_SEND_DOW=WED
OPPORTUNITY_SEND_HOUR=9
OPPORTUNITY_SEND_PROVIDER=gmail_smtp
OPPORTUNITY_EMAIL_SUBJECT=Weekly Job Newsletter
OPPORTUNITY_MAX_JOBS=30
OPPORTUNITY_MIN_JOBS=15
OPPORTUNITY_MAX_JOBS_PER_COMPANY=2
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=Schwarzman Job Updates <schwarzmanjobupdates@gmail.com>
SMTP_USE_STARTTLS=true
```

For Wednesday delivery from 09:00 Beijing time, keep hourly catch-up attempts aligned with the runtime guard. This example checks from 09:17 through 23:17 Beijing time:

```yaml
- cron: "17 1-15 * * WED" # Wednesday 09:17-23:17 Asia/Shanghai
```

The `Check send readiness` and `Run weekly digest` schedule checks in the workflow should compare `github.event.schedule` to that same cron string.

## Manual Sends

Use manual sends for smoke tests and one-off retries:

1. Open **Actions** -> **OpportunityRadar schedule** -> **Run workflow**.
2. Choose `weekly-send`, not `all-preview`.
3. Confirm the run includes the `Check send readiness` step.
4. Confirm Gmail accepted the SMTP send and the Google Group delivered it to members.

`all-preview` and `weekly-preview` are deliberately safe: they build previews but never email the group.

## If Postings Stop

Check the **OpportunityRadar schedule** Actions page first. GitHub automatically
disables scheduled workflows in public repositories after 60 days without repository
activity. Updates in the separate private state repository do not keep this public
repository active. If the page reports inactivity, click **Enable workflow**.

Then run `discovery` and confirm it finishes with `State mutated: True`, followed by
`weekly-preview` to inspect the available jobs before a manual send. A green scheduled
run can simply mean `outside_configured_send_window`; it does not prove email delivery.
Look for `Recipients sent: 1/1` in a send run, and confirm Google Group delivery separately.

GitHub can delay or drop scheduled events. The send hour is the earliest allowed
hour, not a guaranteed delivery time. Hourly attempts can recover later on the
configured local day; an attempt before that hour or after local midnight skips.
`week_already_sent` means the current week's delivery was already recorded.
The weekly cron uses minute 17 to avoid the busiest top-of-hour period; see
[GitHub's schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

If discovery fails in `GithubJsonStore.save`, inspect the HTTP status and GitHub error
details. State uploads use compact JSON and retry temporary server failures up to three
attempts. A persistent validation error needs investigation before retrying delivery.

See [GitHub's workflow enable/disable documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)
for the inactivity policy and recovery controls.

## State And Config Files

Local development reads from files under `data/` by default. Production can read runtime files from a private GitHub state repo.

| File | Purpose |
| --- | --- |
| `data/config/discovery.example.json` | Common Crawl registry and board-polling limits. |
| `data/config/conditions.example.json` | Posting recency, target locations, role groups, exclude terms, and years-of-experience filters. |
| `data/config/sources.example.json` | Optional explicitly configured sources. |
| `docs/opportunity-criteria.md` | Ranking criteria used by the LLM. |
| `data/state/opportunity-state.json` | Local durable state for development runs. |

Production state repo variables can point to private equivalents with `GITHUB_DISCOVERY_PATH`, `GITHUB_CONDITIONS_PATH`, `GITHUB_SOURCES_PATH`, and `GITHUB_STATE_PATH`.

## Local Development

Install dependencies:

```powershell
python -m pip install -r requirements-backend.txt
```

Run fixture-backed discovery without writing state:

```powershell
python scripts\run_discovery.py --root . --sources tests\fixtures\sources.fixture.json --conditions tests\fixtures\conditions.fixture.json --deterministic-fallback --json
```

Preview a fixture-backed weekly digest without spending model tokens:

```powershell
python scripts\run_weekly_digest.py --root . --sources tests\fixtures\sources.fixture.json --deterministic-fallback --include-seen
```

Write evaluated jobs to local state, then preview the digest from state:

```powershell
python scripts\run_discovery.py --root . --sources data\config\sources.greenhouse-smoke.json --conditions data\config\conditions.example.json --deterministic-fallback --write
python scripts\run_weekly_digest.py --root . --from-state
```

Check Gmail SMTP readiness and send from evaluated state:

```powershell
python scripts\check_send_ready.py --root .
python scripts\run_weekly_digest.py --root . --send --from-state
```

Run the protected API locally:

```powershell
python scripts\serve_backend.py --root . --host 127.0.0.1 --port 8765
```

Preview through HTTP:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/digest/preview -Headers @{ Authorization = "Bearer $env:OPPORTUNITY_API_TOKEN" } -Body '{"from_state":true}' -ContentType 'application/json'
```

## Other Delivery Paths

The codebase includes sender implementations for Gmail SMTP, Gmail API, Microsoft Graph email, and Twilio WhatsApp. This README focuses on the intended Gmail SMTP plus Google Groups deployment. Twilio WhatsApp remains the checked-in default, as described in [Current Configuration](#current-configuration); select the desired provider explicitly.

## Verification

Run the production gate before changing scheduling, delivery, or ranking behavior:

```powershell
python scripts\run_production_gate.py --root .
```

The gate compiles Python, runs unit/integration tests, performs fixture-backed registry refresh, exercises dynamic discovery, and runs digest smoke checks.

## Deployment Notes

`render.yaml` defines only the optional free Render web service. It does not create Render cron services because those can require paid cron billing.

Scheduled work is owned by GitHub Actions. Before enabling live sends, run `weekly-send` manually with a test recipient and confirm the Gmail-to-Google-Groups path is ready.

See `docs/cost-controls.md` for budget controls and `docs/private-state-repo-readme.md` for the private state repo layout.
