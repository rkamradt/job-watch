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


def _build_html(jobs: list[ScreenedJob], now: datetime, extra_html: str = "") -> str:
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
  {extra_html}
  <p style="font-size:11px;color:#999;margin-top:24px">
    job-watch · <a href="https://github.com/rkamradt/job-watch">github.com/rkamradt/job-watch</a>
  </p>
</body>
</html>"""


def _build_discovered_plain(discovered: list[dict]) -> str:
    if not discovered:
        return ""
    lines: list[str] = [
        "",
        "=" * 60,
        f"Discovered companies this week ({len(discovered)})",
        "=" * 60,
        "",
    ]
    for d in discovered:
        status = d.get("status", "candidate")
        ats_info = f"{d['ats']}/{d['slug']}" if d.get("ats") else "board not found yet"
        lines.append(f"  {d['name_raw']}  [{status}]  ({ats_info})")
        src = d.get("source", {})
        lines.append(f"    Trigger: {src.get('job_title_trigger', '')}  via {src.get('sender', '')}")
        lines.append("")
    return "\n".join(lines)


def _build_discovered_html(discovered: list[dict]) -> str:
    if not discovered:
        return ""
    rows = []
    for d in discovered:
        status = d.get("status", "candidate")
        badge_color = "#2a6496" if status == "active" else "#888"
        ats_info = f"{d['ats']}/{d['slug']}" if d.get("ats") else "—"
        src = d.get("source", {})
        rows.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:6px 4px"><strong>{d["name_raw"]}</strong>'
            f'  <span style="background:{badge_color};color:#fff;padding:1px 5px;'
            f'border-radius:3px;font-size:10px">{status}</span></td>'
            f'<td style="padding:6px 4px;color:#555;font-size:12px">{ats_info}</td>'
            f'<td style="padding:6px 4px;color:#555;font-size:12px">'
            f'{src.get("job_title_trigger","")}</td>'
            f'</tr>'
        )
    return (
        '<h3 style="margin-top:32px;border-top:1px solid #ccc;padding-top:16px">'
        f'Discovered companies this week ({len(discovered)})</h3>'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr>'
        '<th style="text-align:left;padding:4px">Company</th>'
        '<th style="text-align:left;padding:4px">Board</th>'
        '<th style="text-align:left;padding:4px">Trigger title</th>'
        '</tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
    )


def send_digest(
    jobs: list[ScreenedJob],
    *,
    gmail_user: str,
    gmail_app_password: str,
    to_addr: str,
    discovered: list[dict] | None = None,
) -> None:
    now = datetime.now(tz=timezone.utc)
    subject = _build_subject(jobs)
    plain = _build_plain(jobs, now) + _build_discovered_plain(discovered or [])
    disc_html = _build_discovered_html(discovered or [])
    html = _build_html(jobs, now, extra_html=disc_html)

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
