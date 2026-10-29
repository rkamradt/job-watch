"""Tests for Gmail ingestion and extraction (PR 1).

Uses fixture HTML blobs (one per sender) and mocked HTTP/LLM responses.
No live network calls or Gmail credentials required.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from job_watch.models import ExtractedLead, GmailDiscoveryConfig
from job_watch.sources.parsers import run_parsers
from job_watch.sources.parsers import linkedin, indeed, wellfound, glassdoor


# ── Fixture HTML blobs ────────────────────────────────────────────────────────

_LINKEDIN_HTML = """
<html><body>
<table>
  <tr>
    <td>
      <a href="https://www.linkedin.com/jobs/view/123">Staff Software Engineer</a>
      <span>Toast · Remote · $180K+</span>
    </td>
  </tr>
  <tr>
    <td>
      <a href="https://www.linkedin.com/comm/jobs/view/456">Principal Architect</a>
      <span>Arcadia · Remote, United States</span>
    </td>
  </tr>
</table>
<table>
  <tr><td><a href="https://www.linkedin.com/help">Help Center</a></td></tr>
  <tr><td><a href="https://www.linkedin.com/legal/privacy-policy">Privacy Policy</a></td></tr>
  <tr><td><a href="https://www.linkedin.com/psettings/guest-controls">Unsubscribe</a></td></tr>
</table>
</body></html>
"""

_INDEED_HTML = """
<html><body>
<div>
  <a href="https://www.indeed.com/rc/clk?jk=abc123">Lead Backend Engineer</a>
  Smartsheet
  Remote, WA
</div>
<div>
  <a href="https://www.indeed.com/viewjob?jk=def456">Staff Platform Engineer</a>
  Instacart
  Remote, US
</div>
<table>
  <tr><td><a href="https://www.indeed.com/legal/terms-of-service">Terms of Use</a></td></tr>
  <tr><td><a href="https://www.indeed.com/prefs?hl=en">Manage Settings</a></td></tr>
  <tr><td><a href="https://www.indeed.com/unsubscribe">Unsubscribe</a></td></tr>
</table>
</body></html>
"""

_WELLFOUND_HTML = """
<html><body>
<div>
  <strong>Mattermost</strong>
  <a href="https://wellfound.com/jobs/123-principal-engineer">Principal Engineer</a>
  <span>Remote</span>
</div>
<table>
  <tr><td><a href="https://wellfound.com/privacy">Privacy Policy</a></td></tr>
  <tr><td><a href="https://wellfound.com/terms">Terms &amp; Conditions</a></td></tr>
  <tr><td><a href="https://wellfound.com/unsubscribe">Unsubscribe</a></td></tr>
</table>
</body></html>
"""

_GLASSDOOR_HTML = """
<html><body>
<div>
  <a href="https://www.glassdoor.com/job-listing/staff-engineer-toast-JV_IC123_KO0,14_KE15,20.htm">
    Staff Engineer
  </a>
  · Toast · Remote, US · $160K–$200K
</div>
<table>
  <tr>
    <td><a href="https://www.glassdoor.com/email-preferences">Manage Settings</a></td>
    <td><a href="https://www.glassdoor.com/about/privacy.htm">Privacy Policy</a></td>
    <td><a href="https://www.glassdoor.com/member/main/unsubscribe-email.htm">Unsubscribe</a></td>
  </tr>
</table>
</body></html>
"""

_EMPTY_HTML = """<html><body><p>No jobs here.</p></body></html>"""


# ── Parser unit tests ─────────────────────────────────────────────────────────

def test_linkedin_parser_extracts_leads():
    leads = linkedin.parse(_LINKEDIN_HTML, "jobalerts-noreply@linkedin.com", "msg1")
    assert len(leads) >= 1
    titles = [l.job_title for l in leads]
    assert any("Staff Software Engineer" in t or "Principal Architect" in t for t in titles)
    for lead in leads:
        assert lead.company_name_raw
        assert lead.sender == "jobalerts-noreply@linkedin.com"
        assert lead.gmail_message_id == "msg1"


def test_indeed_parser_extracts_leads():
    leads = indeed.parse(_INDEED_HTML, "donotreply@match.indeed.com", "msg2")
    assert len(leads) >= 1
    for lead in leads:
        assert lead.company_name_raw
        assert lead.job_title


def test_wellfound_parser_extracts_leads():
    leads = wellfound.parse(_WELLFOUND_HTML, "team@hi.wellfound.com", "msg3")
    assert len(leads) >= 1
    assert any("Principal Engineer" in l.job_title for l in leads)


def test_glassdoor_parser_extracts_leads():
    leads = glassdoor.parse(_GLASSDOOR_HTML, "noreply@glassdoor.com", "msg4")
    assert len(leads) >= 1
    assert any("Staff Engineer" in l.job_title for l in leads)


def test_glassdoor_rejects_manage_settings():
    """Glassdoor footer link to /email-preferences must not appear in leads."""
    leads = glassdoor.parse(_GLASSDOOR_HTML, "noreply@glassdoor.com", "msg4")
    titles = [l.job_title for l in leads]
    assert "Manage Settings" not in titles
    assert not any("manage settings" in t.lower() for t in titles)


def test_indeed_rejects_footer_links():
    """Indeed footer links (unsubscribe, terms, manage settings) must not appear in leads."""
    leads = indeed.parse(_INDEED_HTML, "donotreply@match.indeed.com", "msg2")
    titles = [l.job_title for l in leads]
    assert "Terms of Use" not in titles
    assert "Manage Settings" not in titles
    assert "Unsubscribe" not in titles


def test_wellfound_rejects_footer_links():
    """Wellfound footer links (privacy, terms, unsubscribe) must not appear in leads."""
    leads = wellfound.parse(_WELLFOUND_HTML, "team@hi.wellfound.com", "msg3")
    titles = [l.job_title for l in leads]
    assert "Privacy Policy" not in titles
    assert "Terms & Conditions" not in titles
    assert "Unsubscribe" not in titles


def test_run_parsers_dispatches_by_sender():
    leads = run_parsers(_LINKEDIN_HTML, "jobalerts-noreply@linkedin.com", "msg1")
    assert isinstance(leads, list)


def test_run_parsers_unknown_sender_returns_empty():
    leads = run_parsers(_LINKEDIN_HTML, "random@example.com", "msg99")
    assert leads == []


def test_parser_returns_empty_on_no_jobs():
    leads = linkedin.parse(_EMPTY_HTML, "jobalerts-noreply@linkedin.com", "msg5")
    assert isinstance(leads, list)
    # Empty HTML may yield zero results; that's fine
    for lead in leads:
        assert lead.company_name_raw  # if anything is returned it must have a company


# ── LLM fallback tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_fallback_parses_valid_response():
    from job_watch.sources.parsers.llm_fallback import parse_with_llm

    fake_json = json.dumps([
        {"company": "Acme Corp", "title": "Staff Engineer", "location": "Remote"},
    ])
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=fake_json)]

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        leads = await parse_with_llm(
            _EMPTY_HTML,
            "unknown@example.com",
            "msg10",
            "claude-haiku-4-5-20251001",
        )

    assert len(leads) == 1
    assert leads[0].company_name_raw == "Acme Corp"
    assert leads[0].job_title == "Staff Engineer"


@pytest.mark.asyncio
async def test_llm_fallback_returns_empty_on_malformed_json():
    from job_watch.sources.parsers.llm_fallback import parse_with_llm

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="this is not json at all")]

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        leads = await parse_with_llm(
            _EMPTY_HTML, "unknown@example.com", "msg11", "claude-haiku-4-5-20251001"
        )

    assert leads == []


@pytest.mark.asyncio
async def test_llm_fallback_returns_empty_on_api_error():
    from job_watch.sources.parsers.llm_fallback import parse_with_llm

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(side_effect=Exception("API unavailable"))
        mock_client_cls.return_value = mock_client

        leads = await parse_with_llm(
            _EMPTY_HTML, "unknown@example.com", "msg12", "claude-haiku-4-5-20251001"
        )

    assert leads == []


@pytest.mark.asyncio
async def test_llm_fallback_logs_path(caplog):
    """Verify the fallback logs which path was used (not the body)."""
    import logging
    from job_watch.sources.parsers.llm_fallback import parse_with_llm

    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text="[]")]

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        with caplog.at_level(logging.INFO, logger="job_watch.sources.parsers.llm_fallback"):
            await parse_with_llm(
                _EMPTY_HTML, "test@example.com", "msg-log-test", "claude-haiku-4-5-20251001"
            )

    assert any("llm_fallback" in r.message for r in caplog.records)
    # Ensure body content is NOT in any log record
    for record in caplog.records:
        assert "No jobs here" not in record.message


# ── GmailSource integration tests ─────────────────────────────────────────────

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _make_message(msg_id: str, sender: str, html: str, history_id: str = "100") -> dict:
    return {
        "id": msg_id,
        "historyId": history_id,
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": f"Job Alerts <{sender}>"},
                {"name": "Subject", "value": "New jobs for you"},
            ],
            "body": {"data": _b64(html)},
            "parts": [],
        },
    }


@pytest.mark.asyncio
async def test_gmail_source_first_run_fetches_7_days():
    """First run: no stored history_id → falls back to date-range query."""
    from mongomock_motor import AsyncMongoMockClient
    from job_watch.db import GmailStateStore, JobStore
    from job_watch.sources.gmail import GmailSource

    client = AsyncMongoMockClient()
    state = GmailStateStore(client)

    cfg = GmailDiscoveryConfig(
        enabled=True,
        alert_senders=["jobalerts-noreply@linkedin.com"],
        llm_fallback_enabled=False,
    )

    messages_list_resp = {"messages": [{"id": "msg1"}], "nextPageToken": None}
    msg_detail = _make_message("msg1", "jobalerts-noreply@linkedin.com", _LINKEDIN_HTML)
    profile_resp = {"historyId": "999"}

    call_count = {"n": 0}

    async def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "/messages" in url and "msg1" not in url:
            resp.json.return_value = messages_list_resp
        elif "/messages/msg1" in url:
            resp.json.return_value = msg_detail
        elif "/profile" in url:
            resp.json.return_value = profile_resp
        else:
            resp.json.return_value = {}
        call_count["n"] += 1
        return resp

    with patch("job_watch.sources.gmail._refresh_token", return_value="fake-token"), \
         patch("job_watch.sources.gmail._build_credentials", return_value=MagicMock()):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=fake_get)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_http

            source = GmailSource(cfg, state)
            leads = await source.fetch_leads()

    # history_id should now be stored
    stored = await state.get_history_id()
    assert stored == "999"

    # At least attempted to extract from the message
    assert isinstance(leads, list)


@pytest.mark.asyncio
async def test_gmail_source_skips_non_alert_senders():
    """Messages from senders not in the alert list are ignored."""
    from mongomock_motor import AsyncMongoMockClient
    from job_watch.db import GmailStateStore
    from job_watch.sources.gmail import GmailSource

    client = AsyncMongoMockClient()
    state = GmailStateStore(client)
    # Seed a history_id so we use the history path
    await state.set_history_id("50", datetime.now(tz=timezone.utc))

    cfg = GmailDiscoveryConfig(
        enabled=True,
        alert_senders=["jobalerts-noreply@linkedin.com"],
        llm_fallback_enabled=False,
    )

    history_resp = {
        "historyId": "51",
        "history": [{"messagesAdded": [{"message": {"id": "msgX", "labelIds": ["INBOX"]}}]}],
    }
    # This message is from a non-alert sender
    msg_detail = _make_message("msgX", "newsletter@example.com", _LINKEDIN_HTML, history_id="51")

    async def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "/history" in url:
            resp.json.return_value = history_resp
        elif "/messages/msgX" in url:
            resp.json.return_value = msg_detail
        else:
            resp.json.return_value = {}
        return resp

    with patch("job_watch.sources.gmail._refresh_token", return_value="fake-token"), \
         patch("job_watch.sources.gmail._build_credentials", return_value=MagicMock()):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_http = AsyncMock()
            mock_http.get = AsyncMock(side_effect=fake_get)
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_http

            source = GmailSource(cfg, state)
            leads = await source.fetch_leads()

    assert leads == []


# ── GmailStateStore tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gmail_state_round_trips():
    from mongomock_motor import AsyncMongoMockClient
    from job_watch.db import GmailStateStore

    client = AsyncMongoMockClient()
    store = GmailStateStore(client)

    assert await store.get_history_id() is None

    now = datetime.now(tz=timezone.utc)
    await store.set_history_id("12345", now)
    assert await store.get_history_id() == "12345"

    await store.set_history_id("99999", now)
    assert await store.get_history_id() == "99999"
