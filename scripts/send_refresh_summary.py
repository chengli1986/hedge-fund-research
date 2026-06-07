"""Render + send the monthly profile-refresh summary email.

render_summary() is pure (testable); send() does SMTP via ~/.stock-monitor.env.
Notification only — callers must not let this affect their exit code.
"""
from __future__ import annotations
import argparse, html, os, smtplib, sys
from email.mime.text import MIMEText


def render_summary(*, applied: list[str], flagged: list[str], alert_only: bool) -> str:
    def li(items):
        return "".join(f"<li>{html.escape(x)}</li>" for x in items) or "<li>—</li>"
    banner = ('<p style="background:#fff3cd;padding:8px;border-radius:6px">'
              '⚠️ ALERT-ONLY 告警模式：未自动应用，请人工确认。</p>' if alert_only else "")
    if not applied and not flagged:
        body = "<p>本月无变化（no change）。</p>"
    else:
        body = (f"<h3>已自动应用 ({len(applied)})</h3><ul>{li(applied)}</ul>"
                f"<h3>转人工 ({len(flagged)})</h3><ul>{li(flagged)}</ul>")
    return (f"<html><head><meta charset='utf-8'><title>GMIA Profile Refresh</title></head>"
            f"<body style='font-family:sans-serif'>{banner}"
            f"<h2>GMIA 月度 Profile 刷新</h2>{body}</body></html>")


def send(subject: str, html_body: str) -> bool:
    host = os.environ.get("SMTP_HOST"); user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS"); to = os.environ.get("MAIL_TO", user)
    if not all([host, user, pw, to]):
        sys.stderr.write("[send_refresh_summary] SMTP env missing; skip\n"); return False
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
    msg["MIME-Version"] = "1.0"
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--applied", default=""); ap.add_argument("--flagged", default="")
    ap.add_argument("--alert-only", default="1")
    a = ap.parse_args()
    # Split on newline (not whitespace): flagged entries contain spaces, e.g.
    # "man-group (apply_refresh rc=1)".
    applied = [x.strip() for x in a.applied.split("\n") if x.strip()]
    flagged = [x.strip() for x in a.flagged.split("\n") if x.strip()]
    alert_only = a.alert_only == "1"
    html_body = render_summary(applied=applied, flagged=flagged, alert_only=alert_only)
    send(f"GMIA Profile Refresh — {len(applied)} applied / {len(flagged)} flagged", html_body)
    print(f"[send_refresh_summary] rendered ({len(applied)+len(flagged)} items), alert_only={alert_only}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
