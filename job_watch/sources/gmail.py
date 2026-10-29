"""Gmail job-alert ingestion source.

Reads job-alert emails via the Gmail API (gmail.readonly scope only).
Uses an OAuth2 refresh-token stored in environment variables — no interactive
flow runs in-cluster.

Required env vars (from K8s secret job-watch-gmail-creds):
  GMAIL_CLIENT_ID
  GMAIL_CLIENT_SECRET
  GMAIL_REFRESH_TOKEN

The credentials are exchanged for a short-lived access token on each call.

History tracking:
  - On first run, fetches messages from the last 7 days.
  - Subsequent runs call users.history.list from the stored historyId so
    each message is processed exactly once.
  - The current historyId is persisted in MongoDB (gmail_state collection).

Never logs email bodies.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

from job_watch.db import GmailStateStore
from job_watch.models import ExtractedLead, GmailDiscoveryConfig
from job_watch.sources.parsers import run_parsers
from job_watch.sources.parsers.llm_fallback import parse_with_llm

logger = logging.getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _build_credentials():
    import google.oauth2.credentials  # lazy: avoids importing requests at module load
    return google.oauth2.credentials.Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
    )


def _refresh_token(creds) -> str:
    import google.auth.transport.requests  # lazy: requires requests; not needed in tests
    request = google.auth.transport.requests.Request()
    creds.refresh(request)
    return creds.token


async def _gmail_get(
    client: httpx.AsyncClient,
    access_token: str,
    path: str,
    params: dict | None = None,
) -> dict:
    url = f"{_GMAIL_BASE}/{path}"
    resp = await client.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
    )
    resp.raise_for_status()
    return resp.json()


def _decode_body(payload: dict) -> str:
    """Recursively extract the HTML (preferred) or plain-text body from a message payload."""
    mime = payload.get("mimeType", "")
    if mime in ("text/html", "text/plain"):
        data = payload.get("body", {}).get("data", "")
        if data:
            raw = base64.urlsafe_b64decode(data + "==")
            return raw.decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _decode_body(part)
        if result:
            return result
    return ""


def _extract_sender(headers: list[dict]) -> str:
    for h in headers:
        if h.get("name", "").lower() == "from":
            val = h.get("value", "")
            # Extract the address from "Display Name <addr@example.com>"
            if "<" in val and ">" in val:
                return val.split("<")[1].rstrip(">").strip().lower()
            return val.strip().lower()
    return ""


async def _fetch_message_ids_since(
    client: httpx.AsyncClient,
    access_token: str,
    sender_list: list[str],
    days: int = 7,
) -> list[str]:
    """Fetch message IDs for alert emails from the last N days."""
    after_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp())
    query = f"after:{after_ts} ({' OR '.join(f'from:{s}' for s in sender_list)})"
    data = await _gmail_get(
        client, access_token, "messages",
        params={"q": query, "maxResults": "500"},
    )
    return [m["id"] for m in data.get("messages", [])]


async def _fetch_new_message_ids_via_history(
    client: httpx.AsyncClient,
    access_token: str,
    history_id: str,
    sender_set: set[str],
) -> tuple[list[str], str]:
    """Return (new_message_ids, latest_history_id) using the history API."""
    data = await _gmail_get(
        client, access_token, "history",
        params={"startHistoryId": history_id, "historyTypes": "messageAdded", "maxResults": "500"},
    )
    new_history_id = data.get("historyId", history_id)
    message_ids: list[str] = []
    for record in data.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added.get("message", {})
            # Quick pre-filter by label (INBOX) before fetching full message
            labels = set(msg.get("labelIds", []))
            if "INBOX" not in labels:
                continue
            message_ids.append(msg["id"])
    return message_ids, new_history_id


async def _get_current_history_id(
    client: httpx.AsyncClient,
    access_token: str,
) -> str:
    data = await _gmail_get(client, access_token, "profile")
    return str(data.get("historyId", ""))


class GmailSource:
    """Reads job-alert emails and returns extracted leads."""

    def __init__(self, cfg: GmailDiscoveryConfig, state: GmailStateStore) -> None:
        self._cfg = cfg
        self._state = state

    async def fetch_leads(self) -> list[ExtractedLead]:
        creds = _build_credentials()
        access_token = _refresh_token(creds)
        now = datetime.now(tz=timezone.utc)
        sender_set = {s.lower() for s in self._cfg.alert_senders}

        async with httpx.AsyncClient(timeout=30) as client:
            stored_history_id = await self._state.get_history_id()

            if stored_history_id is None:
                # First run: fetch last 7 days
                logger.info("gmail_source: first run, fetching last 7 days of alerts")
                message_ids = await _fetch_message_ids_since(
                    client, access_token, self._cfg.alert_senders, days=7
                )
                current_history_id = await _get_current_history_id(client, access_token)
            else:
                logger.info("gmail_source: fetching new messages since history_id=%s", stored_history_id)
                try:
                    message_ids, current_history_id = await _fetch_new_message_ids_via_history(
                        client, access_token, stored_history_id, sender_set
                    )
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        # History ID expired (>30 days); fall back to 7-day window
                        logger.warning(
                            "gmail_source: history_id expired, falling back to 7-day window"
                        )
                        message_ids = await _fetch_message_ids_since(
                            client, access_token, self._cfg.alert_senders, days=7
                        )
                        current_history_id = await _get_current_history_id(client, access_token)
                    else:
                        raise

            logger.info("gmail_source: %d candidate message(s) to process", len(message_ids))

            leads: list[ExtractedLead] = []
            for msg_id in message_ids:
                msg_leads = await self._process_message(client, access_token, msg_id, sender_set)
                leads.extend(msg_leads)

            await self._state.set_history_id(current_history_id, now)
            logger.info("gmail_source: %d lead(s) extracted, history_id updated to %s",
                        len(leads), current_history_id)
            return leads

    async def _process_message(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        msg_id: str,
        sender_set: set[str],
    ) -> list[ExtractedLead]:
        try:
            data = await _gmail_get(client, access_token, f"messages/{msg_id}",
                                    params={"format": "full"})
        except Exception as exc:
            logger.warning("gmail_source: failed to fetch message_id=%s: %s", msg_id, exc)
            return []

        headers = data.get("payload", {}).get("headers", [])
        sender = _extract_sender(headers)
        if sender not in sender_set:
            return []

        html_body = _decode_body(data.get("payload", {}))
        if not html_body:
            logger.debug("gmail_source: empty body for message_id=%s sender=%s", msg_id, sender)
            return []

        leads = run_parsers(html_body, sender, msg_id)

        if not leads and self._cfg.llm_fallback_enabled:
            leads = await parse_with_llm(
                html_body, sender, msg_id, self._cfg.llm_fallback_model
            )
        elif not leads:
            logger.debug(
                "parsers: no leads and llm_fallback disabled for sender=%s message_id=%s",
                sender,
                msg_id,
            )

        return leads
