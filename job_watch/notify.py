"""Gmail SMTP digest sender."""

from __future__ import annotations

import logging
import smtplib
from collections import Counter
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from job_watch.filter import ScoredJob

logger = logging.getLogger(__name__)

_GMAIL_SMTP = "smtp.gmail.com"
_GMAIL_PORT = 587


def _build_subject(jobs: list[ScoredJob]) -> str:
    companies = [s.job.company for s in jobs]
    top = [c for c, _ in Counter(companies).most_common(3)]
    n = len(jobs)
    return f"job-watch: {n} new match{'es' if n != 1 else ''} ({', '.join(top)})"


def _build_plain(jobs: list[ScoredJob], now: datetime) -> str:
    lines: list[str] = [
        f"New job matches — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
    ]
    for s in jobs:
        j = s.job
        lines.append(f"[{j.company}] {j.title}")
        lines.append(f"  Location:   {j.location or 'Not specified'}")
        if j.department:
            lines.append(f"  Department: {j.department}")
        if s.score:
            lines.append(f"  Keywords:   {', '.join(s.matched_keywords)} (score {s.score})")
        lines.append(f"  Apply:      {j.url}")
        lines.append("")
    lines.append("--")
    lines.append("job-watch · https://github.com/rkamradt/job-watch")
    return "\n".join(lines)


def _build_html(jobs: list[ScoredJob], now: datetime) -> str:
    rows: list[str] = []
    for s in jobs:
        j = s.job
        kw_html = ""
        if s.score:
            kw_html = (
                f'<br><small style="color:#666">Keywords: '
                f'{", ".join(s.matched_keywords)} (score {s.score})</small>'
            )
        rows.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:8px 4px"><strong>{j.company}</strong></td>'
            f'<td style="padding:8px 4px">'
            f'  <a href="{j.url}">{j.title}</a>'
            f'  <br><small style="color:#444">{j.location or "Not specified"}'
            f'  {"· " + j.department if j.department else ""}</small>'
            f'  {kw_html}'
            f'</td>'
            f'</tr>'
        )
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!DOCTYPE html>
<html>
<body style="font-family:sans-serif;max-width:700px;margin:auto;padding:16px">
  <h2 style="border-bottom:2px solid #333">job-watch · New matches</h2>
  <p style="color:#555">{timestamp}</p>
  <table style="width:100%;border-collapse:collapse">
    <thead>
      <tr>
        <th style="text-align:left;padding:4px;width:140px">Company</th>
        <th style="text-align:left;padding:4px">Role</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  <p style="font-size:11px;color:#999;margin-top:24px">
    job-watch · <a href="https://github.com/rkamradt/job-watch">github.com/rkamradt/job-watch</a>
  </p>
</body>
</html>"""


def send_digest(
    jobs: list[ScoredJob],
    *,
    gmail_user: str,
    gmail_app_password: str,
    to_addr: str,
) -> None:
    now = datetime.now(tz=timezone.utc)
    subject = _build_subject(jobs)
    plain = _build_plain(jobs, now)
    html = _build_html(jobs, now)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    logger.info("Sending digest to %s: %s", to_addr, subject)
    with smtplib.SMTP(_GMAIL_SMTP, _GMAIL_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_app_password)
        smtp.sendmail(gmail_user, [to_addr], msg.as_string())
    logger.info("Digest sent.")
