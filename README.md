# job-watch

Monitors ATS job boards (Greenhouse, Lever, Ashby) for new senior/principal engineering
roles, scores them against your keyword criteria, optionally AI-screens them with Claude,
and emails you a digest of new matches.

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
  - { name: Toast, ats: greenhouse, slug: toast }
  - { name: Arcadia, ats: lever, slug: arcadia }

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
```

---

## MCP server — ask Claude about your job leads

job-watch exposes its data as an MCP server so you can query your job pipeline
directly from Claude Code or Claude Desktop.

### Tools

| Tool | Description |
|------|-------------|
| `search_jobs` | Keyword search over title + description; filter by company, recency, remote, review status |
| `get_job` | Full record including description and apply URL |
| `new_since` | Jobs first seen in the last N days |
| `list_companies` | Configured companies with open job counts |
| `stats` | Counts by company, by review status, new this week |
| `mark_job` | Set review status (new/interested/applied/skipped) + optional note |
| `suggest_company` | Validate an ATS slug and get the YAML snippet to add — does not edit config |

### Resources

- `resource://job-watch/filter-criteria` — your title filters, boost keywords, and candidate profile

### Prompts

- `weekly-review` — guided session: summarizes new jobs and walks you through marking each one

### Connecting to Claude Code (stdio — local)

```bash
# From the job-watch repo root with MONGO_URI set in your environment:
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

Deploy `Dockerfile.mcp` as a separate pod (see Helm chart stub in the cluster deployment
notes). Once running at `https://job-watch-mcp.kamradtfamily.net`:

```bash
claude mcp add job-watch-remote --transport http https://job-watch-mcp.kamradtfamily.net/mcp
```

---

## Example questions

```
"What new principal or staff roles showed up this week?"
→ Calls new_since(7), lists titles, companies, locations.

"Which of these look most like the Arcadia agent-platform role?"
→ Reads the Arcadia job via get_job, then searches for similar descriptions.

"Show me remote roles mentioning Kafka that I haven't reviewed yet."
→ Calls search_jobs(query="Kafka", remote_only=True, status="new").

"Run the weekly review."
→ Invokes the weekly-review prompt: stats → new jobs → mark each one.
```

---

## Review statuses

| Status | Meaning |
|--------|---------|
| `new` | Not yet reviewed (default for all jobs) |
| `interested` | Worth applying |
| `applied` | Application submitted |
| `skipped` | Not a fit |

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

Tests use `mongomock-motor` — no live MongoDB needed.

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MONGO_URI` | Yes | — | MongoDB connection string |
| `GMAIL_USER` | For email | — | Gmail address to send from |
| `GMAIL_APP_PASSWORD` | For email | — | Gmail app password |
| `GMAIL_TO` | No | `randykamradt@gmail.com` | Recipient |
| `ANTHROPIC_API_KEY` | If screening enabled | — | Claude API key |
| `JOB_WATCH_CONFIG` | No | `./companies.yaml` | Path to config file |
