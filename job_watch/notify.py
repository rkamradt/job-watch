"""Gmail SMTP digest sender."""

from __future__ import annotations

import logging
import smtplib
from collections import Counter
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from job_watch.screener import ScreenedJob

logger = logging.getLogger(__name__)

_GMAIL_SMTP = "smtp.gmail.com"
_GMAIL_PORT = 587


def _build_subject(jobs: list[ScreenedJob]) -> str:
    companies = [s.scored.job.company for s in jobs]
    top = [c for c, _ in Counter(companies).most_common(3)]
    n = len(jobs)
    return f"job-watch: {n} new match{'es' if n != 1 else ''} ({', '.join(top)})"


def _build_plain(jobs: list[ScreenedJob], now: datetime) -> str:
    lines: list[str] = [
        f"New job matches — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 60,
        "",
    ]
    for item in jobs:
        s = item.scored
        j = s.job
        score_tag = f"[score {item.llm_score}{'*' if item.used_fallback else ''}]"
        lines.append(f"{score_tag} [{j.company}] {j.title}")
        lines.append(f"  Location:   {j.location or 'Not specified'}")
        if j.department:
            lines.append(f"  Department: {j.department}")
        if s.score:
            lines.append(f"  Keywords:   {', '.join(s.matched_keywords)} ({s.score} matched)")
        if item.salary_min:
            lines.append(f"  Salary min: ${item.salary_min:,}")
        if item.ai_reason and not item.used_fallback:
            lines.append(f"  Fit note:   {item.ai_reason}")
        lines.append(f"  Apply:      {j.url}")
        lines.append("")
    lines.append("--")
    lines.append("job-watch · https://github.com/rkamradt/job-watch")
    lines.append("* score marked with * used keyword fallback (LLM unavailable)")
    return "\n".join(lines)


def _score_badge(item: ScreenedJob) -> str:
    score = item.llm_score
    if item.used_fallback:
        color = "#888"
    elif score >= 85:
        color = "#2d7a2d"  # dark green
    elif score >= 70:
        color = "#2a6496"  # blue
    else:
        color = "#888"
    suffix = "*" if item.used_fallback else ""
    return (
        f'<span style="background:{color};color:#fff;padding:2px 6px;'
        f'border-radius:3px;font-size:11px;font-weight:bold">'
        f'{score}{suffix}</span>'
    )


def _build_html(jobs: list[ScreenedJob], now: datetime) -> str:
    rows: list[str] = []
    for item in jobs:
        s = item.scored
        j = s.job
        badge = _score_badge(item)
        kw_html = ""
        if s.score:
            kw_html = (
                f'<br><small style="color:#666">Keywords: '
                f'{", ".join(s.matched_keywords)} ({s.score} matched)</small>'
            )
        salary_html = ""
        if item.salary_min:
            salary_html = (
                f'<br><small style="color:#2d7a2d">Min salary: ${item.salary_min:,}</small>'
            )
        ai_html = ""
        if item.ai_reason and not item.used_fallback:
            ai_html = (
                f'<br><small style="color:#2a6496;font-style:italic">'
                f'&#x1F916; {item.ai_reason}</small>'
            )
        rows.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:8px 4px;vertical-align:top">'
            f'  {badge}<br>'
            f'  <strong>{j.company}</strong>'
            f'</td>'
            f'<td style="padding:8px 4px">'
            f'  <a href="{j.url}">{j.title}</a>'
            f'  <br><small style="color:#444">{j.location or "Not specified"}'
            f'  {"· " + j.department if j.department else ""}</small>'
            f'  {kw_html}'
            f'  {salary_html}'
            f'  {ai_html}'
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
    jobs: list[ScreenedJob],
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
