# Claude Code Instructions — job-watch

Python asyncio ATS job-board poller with LLM scoring and Gmail discovery.
Runs in K3s as a CronJob; also exposes an MCP server for Claude integration.

---

## Architecture

```
companies.yaml ──► main.py ──► ATS adapters (Greenhouse/Lever/Ashby)
                        │              ↓
                        │         filter.py  (keyword filter + score)
                        │              ↓
                        │         screener.py  (LLM 0–100 fit score, cached)
                        │              ↓
                        │         notify.py  (HTML/plain email digest)
                        │
                        └──► gmail.py ──► sources/parsers/  (per-sender)
                                   │              ↓ (fallback)
                                   │         llm_fallback.py
                                   ↓
                             discovery/resolver.py  (probe ATS boards)
                                   ↓
                             db.py  (discovered_companies collection)

mcp/server.py ──► db.py  (read/write tools for Claude)
```

---

## Key files

| File | Purpose |
|------|---------|
| `job_watch/main.py` | CLI entry; orchestrates fetch → filter → screen → notify |
| `job_watch/config.py` | Loads `companies.yaml`; returns `AppConfig` |
| `job_watch/models.py` | Pydantic models: `AppConfig`, `Job`, `LlmScoringConfig`, `GmailDiscoveryConfig`, `ExtractedLead` |
| `job_watch/db.py` | Motor async MongoDB: `JobStore`, `DiscoveredCompanyStore`, `GmailStateStore`, `ScoringUsageStore` |
| `job_watch/adapters/` | ATS adapters: `greenhouse.py`, `lever.py`, `ashby.py` |
| `job_watch/filter.py` | Keyword title filter + boost scoring → `ScoredJob` |
| `job_watch/screener.py` | LLM 0–100 scoring with cache, daily cap, fallback → `ScreenedJob` |
| `job_watch/notify.py` | Build + send HTML/plain email digest |
| `job_watch/sources/gmail.py` | Gmail OAuth2 incremental fetch → `ExtractedLead` list |
| `job_watch/sources/parsers/` | Per-sender BeautifulSoup parsers (LinkedIn, Indeed, Wellfound, Glassdoor) |
| `job_watch/sources/parsers/llm_fallback.py` | Haiku fallback when no parser matches |
| `job_watch/discovery/resolver.py` | `ATSResolver.probe()`: iterates ATS × slugs to find a board |
| `job_watch/mcp/server.py` | FastMCP server; stdio + streamable-HTTP transports |
| `companies.yaml` | Runtime config (mounted as ConfigMap in K8s) |
| `scripts/gmail_auth.py` | One-time OAuth2 flow; prints `kubectl create secret` command |

---

## MongoDB collections

| Collection | Store class | Purpose |
|------------|-------------|---------|
| `seen_jobs` | `JobStore` | All fetched job postings; `llm_score` subdoc when scored |
| `discovered_companies` | `DiscoveredCompanyStore` | Companies found in alert emails |
| `gmail_state` | `GmailStateStore` | Singleton: Gmail `historyId` for incremental fetch |
| `scoring_usage` | `ScoringUsageStore` | Daily LLM call counts, token usage, estimated cost |

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MONGO_URI` | Yes | MongoDB connection string |
| `GMAIL_USER` | For email | Gmail SMTP sender address |
| `GMAIL_APP_PASSWORD` | For email | Gmail app password (SMTP) |
| `GMAIL_TO` | No | Digest recipient (default: `randykamradt@gmail.com`) |
| `ANTHROPIC_API_KEY` | If screening/scoring on | Claude API key |
| `GMAIL_CLIENT_ID` | If gmail_discovery on | OAuth2 client ID |
| `GMAIL_CLIENT_SECRET` | If gmail_discovery on | OAuth2 client secret |
| `GMAIL_REFRESH_TOKEN` | If gmail_discovery on | OAuth2 refresh token |
| `JOB_WATCH_CONFIG` | No | Path to `companies.yaml` (default: auto-detected) |

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use `mongomock-motor` — no live MongoDB needed.
`pytest-asyncio` in auto mode; no `@pytest.mark.asyncio` needed on individual tests
(mode is set in `pyproject.toml`).

---

## companies.yaml top-level sections

```yaml
companies:      # list of {name, ats, slug}
filter:         # title_include, title_engineering, title_exclude, boost_keywords,
                # location_keep_patterns, location_drop_patterns
screening:      # enabled, model, candidate_profile
llm_scoring:    # enabled, threshold, daily_cap, model, remote_us_required, dealbreakers
gmail_discovery: # enabled, auto_activate, probe_rate_limit_delay, negative_cache_ttl_days
                 # credentials from env: GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN
```

---

## MCP tools

| Tool | Description |
|------|-------------|
| `search_jobs` | Keyword + filter search over open jobs |
| `get_job` | Full record by `job_id` |
| `new_since` | Jobs first seen in last N days |
| `list_companies` | Configured companies + open job counts |
| `stats` | Aggregate counts; includes `scored_jobs` + `avg_llm_score` |
| `mark_job` | Set review status: new/interested/applied/skipped |
| `list_discovered` | Companies found in alert emails (filter by status) |
| `set_company_status` | Approve (`active`) or reject a discovered company |
| `suggest_company` | Validate ATS slug; returns YAML snippet |

MCP resources: `resource://job-watch/filter-criteria`, `resource://job-watch/scoring-profile`

---

## Deployment (K3s)

- CronJob in namespace `job-watch`
- `companies.yaml` mounted as ConfigMap at `/config/companies.yaml`
- Secrets: `job-watch-secrets` (MONGO_URI, GMAIL_*, ANTHROPIC_API_KEY)
- MCP server runs as a separate Deployment; exposed at `job-watch-mcp.kamradtfamily.net/mcp`
  via Cloudflare tunnel (streamable-HTTP transport)

---

## Gotchas

- **Ashby API key**: `data["jobs"]` not `data["jobPostings"]` — the Ashby board endpoint returns `"jobs"`.
- **LLM scoring fallback**: when daily cap is hit or LLM call fails, `used_fallback=True` and score ≤ 80 (capped to distinguish from LLM scores). Fallback scores show `*` in the digest.
- **remote_us gate**: when `llm_scoring.remote_us_required: true`, jobs where LLM returns `remote_us: false` are dropped before emailing, even if score ≥ threshold.
- **Gmail history API**: on first run (no stored `historyId`), falls back to listing messages from the last 7 days. After that, uses incremental history to avoid re-processing.
- **Lazy Google imports**: `google.auth.transport.requests` is imported inside functions, not at module top, so tests can run without the `requests` package being available at import time.
- **mongomock timezone**: mongomock returns timezone-naive datetimes; normalize with `.replace(tzinfo=timezone.utc)` when comparing.
