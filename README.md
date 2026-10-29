# job-watch

Monitors ATS job boards (Greenhouse, Lever, Ashby) for new senior/principal engineering
roles, scores them with an LLM (0–100 fit score), and emails a digest of new matches.
Optionally discovers new companies to watch from your job-alert emails (LinkedIn, Indeed,
Wellfound, Glassdoor).

---

## Quick start

```bash
# Install
pip install -e ".[dev]"

# Verify a company slug before adding it
job-watch check-slug greenhouse toast

# Seed state (saves jobs without emailing — use on first run)
MONGO_URI=mongodb://... job-watch --seed

# Normal run
MONGO_URI=mongodb://... GMAIL_USER=... GMAIL_APP_PASSWORD=... job-watch
```

---

## Configuration

Edit `companies.yaml`. Mount it as a ConfigMap at `/config/companies.yaml` in Kubernetes.

```yaml
companies:
  - { name: Toast,    ats: greenhouse, slug: toast }
  - { name: Arcadia,  ats: lever,      slug: arcadia }
  - { name: "Hims & Hers", ats: ashby, slug: hims-and-hers }

filter:
  title_include: [principal, staff, lead]
  title_engineering: [engineer, developer, architect]
  title_exclude: [manager, director, intern]
  boost_keywords: [kafka, kubernetes, java, llm]

screening:
  enabled: true
  model: claude-haiku-4-5-20251001
  candidate_profile: |
    20+ years of software engineering...

llm_scoring:
  enabled: true             # 0-100 fit score; replaces PASS/FAIL when enabled
  threshold: 70             # jobs below this score are not emailed
  daily_cap: 200
  model: claude-haiku-4-5-20251001
  remote_us_required: true  # drops jobs where LLM determines remote=non-US
  dealbreakers:
    - "rust-only shop"
    - "on-site only"

gmail_discovery:
  enabled: false            # reads job-alert emails and discovers new companies
  auto_activate: false      # true → auto-add companies whose ATS board is found
  probe_rate_limit_delay: 2.0
  negative_cache_ttl_days: 30
  # Credentials: set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
  # Run scripts/gmail_auth.py once locally to generate them.
```

---

## Gmail discovery setup (one-time)

When `gmail_discovery.enabled: true`, job-watch reads your job-alert emails
(LinkedIn, Indeed, Wellfound, Glassdoor) and probes Greenhouse/Lever/Ashby for
each new company it finds.

```bash
# Install the one-time auth helper
pip install google-auth-oauthlib

# Run the auth flow (opens your browser)
python scripts/gmail_auth.py --credentials ~/Downloads/client_secret_....json

# Create the Kubernetes secret with the printed values
kubectl create secret generic job-watch-gmail-creds \
  --from-literal=GMAIL_CLIENT_ID="..." \
  --from-literal=GMAIL_CLIENT_SECRET="..." \
  --from-literal=GMAIL_REFRESH_TOKEN="..." \
  -n job-watch
```

Prerequisites: GCP project with Gmail API enabled, OAuth 2.0 Desktop app credentials,
your Gmail address added as a Test user on the OAuth consent screen.

---

## MCP server — ask Claude about your job leads

job-watch exposes its data as an MCP server so you can query your job pipeline
directly from Claude Code or Claude Desktop.

### Tools

| Tool | Description |
|------|-------------|
| `search_jobs` | Keyword search over title + description; filter by company, recency, remote, review status |
| `get_job` | Full record including description, apply URL, and llm_score |
| `new_since` | Jobs first seen in the last N days |
| `list_companies` | Configured companies with open job counts |
| `stats` | Counts by company, by review status, LLM scoring coverage (scored_jobs, avg_llm_score) |
| `mark_job` | Set review status (new/interested/applied/skipped) + optional note |
| `suggest_company` | Validate an ATS slug and get the YAML snippet to add |
| `list_discovered` | Companies found in job-alert emails (filter by candidate/active/rejected) |
| `set_company_status` | Approve (`active`) or reject a discovered company |

### Resources

| Resource | Description |
|----------|-------------|
| `resource://job-watch/filter-criteria` | Title filters, boost keywords, candidate profile |
| `resource://job-watch/scoring-profile` | LLM scoring config: threshold, daily cap, dealbreakers |

### Prompts

- `weekly-review` — guided session: stats → new jobs → mark each one

### Connecting to Claude Code (stdio — local)

```bash
claude mcp add job-watch -- env MONGO_URI="$MONGO_URI" job-watch mcp

# Or using the direct script entry point:
claude mcp add job-watch -- env MONGO_URI="$MONGO_URI" job-watch-mcp
```

For Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "job-watch": {
      "command": "job-watch-mcp",
      "env": {
        "MONGO_URI": "mongodb://...",
        "JOB_WATCH_CONFIG": "/path/to/companies.yaml"
      }
    }
  }
}
```

### Connecting to the cluster (streamable HTTP)

```bash
claude mcp add job-watch-remote --transport http https://job-watch-mcp.kamradtfamily.net/mcp
```

---

## Email digest

- Jobs sorted by LLM score descending (or keyword score when scoring disabled)
- Score badge: dark green ≥85, blue ≥70, grey = keyword fallback
- Shows salary minimum when stated in the posting
- Shows LLM fit note for scored jobs
- Footer section: "Discovered companies this week" listing new candidates/actives
  from Gmail discovery (when enabled)

---

## Example questions

```
"What new principal or staff roles showed up this week?"
→ Calls new_since(7), lists titles, companies, locations, scores.

"Show me high-scoring remote Kafka roles I haven't reviewed yet."
→ search_jobs(query="Kafka", remote_only=True, status="new")

"What companies were discovered from my job alerts this week?"
→ list_discovered(status="candidate")

"Approve Stripe as a company to watch."
→ set_company_status("stripe", "active")

"Run the weekly review."
→ Invokes the weekly-review prompt.
```

---

## Review statuses

| Status | Meaning |
|--------|---------|
| `new` | Not yet reviewed (default) |
| `interested` | Worth applying |
| `applied` | Application submitted |
| `skipped` | Not a fit |

---

## MongoDB collections

| Collection | Purpose |
|------------|---------|
| `seen_jobs` | All fetched job postings; includes `llm_score` subdoc when scored |
| `discovered_companies` | Companies found in job-alert emails; tracks probe results |
| `gmail_state` | Singleton: Gmail historyId for incremental message fetch |
| `scoring_usage` | Daily LLM call counts, token usage, estimated cost |

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONGO_URI` | Yes | — | MongoDB connection string |
| `GMAIL_USER` | For email | — | Gmail address to send from (SMTP) |
| `GMAIL_APP_PASSWORD` | For email | — | Gmail app password (SMTP) |
| `GMAIL_TO` | No | `randykamradt@gmail.com` | Digest recipient |
| `ANTHROPIC_API_KEY` | If screening/scoring enabled | — | Claude API key |
| `GMAIL_CLIENT_ID` | If gmail_discovery enabled | — | OAuth2 client ID |
| `GMAIL_CLIENT_SECRET` | If gmail_discovery enabled | — | OAuth2 client secret |
| `GMAIL_REFRESH_TOKEN` | If gmail_discovery enabled | — | OAuth2 refresh token |
| `JOB_WATCH_CONFIG` | No | `./companies.yaml` | Path to config file |

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use `mongomock-motor` — no live MongoDB needed.
