"""Render + send the monthly profile-refresh summary email.

render_summary() is pure (testable); send() does SMTP with the SMTP_USER /
SMTP_PASS / MAIL_TO the wrapper reads from ~/.stock-monitor.env. The host
defaults to smtp.163.com like every other GMIA mail: that file has no
SMTP_HOST, and until the stage-4 audit (2026-10-09) all seven monthly runs
logged "SMTP env missing; skip" and nobody heard what the refresh changed.
Notification only — callers must not let this affect their exit code; the
non-zero exit when nothing was sent is for the wrapper's log line.
"""
from __future__ import annotations
import argparse, difflib, html, json, os, re, smtplib, sys
from email.mime.text import MIMEText
from pathlib import Path


def load_details(ids: list[str], drafts_dir: Path) -> dict[str, list[dict]]:
    """Each fund's change_log: from the archived draft (applied/<id>.refresh.json)
    after a real apply, else from the pending draft (a dry run archives none).
    The draft's `old` is the value apply_refresh checked against publish.py."""
    out: dict[str, list[dict]] = {}
    for fid in ids:
        for path in (Path(drafts_dir) / "applied" / f"{fid}.refresh.json", Path(drafts_dir) / f"{fid}.refresh.json"):
            try:
                log = json.loads(path.read_text(encoding="utf-8")).get("change_log")
            except (OSError, ValueError, AttributeError):
                continue
            if isinstance(log, list):
                out[fid] = [c for c in log if isinstance(c, dict)]
                break
    return out


def load_names(sources: Path) -> dict[str, str]:
    try:
        return {s["id"]: s.get("name", s["id"]) for s in json.loads(Path(sources).read_text(encoding="utf-8"))["sources"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _source(value) -> str:
    text = html.escape(str(value or ""))
    if str(value or "").startswith(("https://", "http://")):
        return f'<a href="{text}" style="word-break:break-all">{text}</a>'
    return text or "—"


LONG_VALUE = 60     # chars; past this a value is a paragraph and gets a marked diff
CONTEXT = 20        # unchanged chars kept on each side of a change; the rest is "…"
_TOKEN = re.compile(r"[A-Za-z0-9$~.,%]+|\s+|.", re.S)


def _marked_diff(old: str, new: str) -> str:
    """One copy of the text with the removed part struck and the added part in
    bold, so a desc_zh edit that only changed "$178B" to "$233B" reads as that
    and not as two 140-char paragraphs side by side (10-10 preview). Tokens are
    ASCII words/numbers or single characters (Chinese has no spaces)."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out = []
    for i, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            text = "".join(a[i1:i2])
            first, last = i == 0, i == len(ops) - 1
            keep = CONTEXT * (1 if first or last else 2)
            if len(text) <= keep:
                out.append(html.escape(text))
            else:
                head = "" if first else text[:CONTEXT]      # context after the previous change
                tail = "" if last else text[-CONTEXT:]      # context before the next change
                out.append(html.escape(head) + "…" + html.escape(tail))
            continue
        if i2 > i1:
            out.append(f"<del style='color:#cf222e'>{html.escape(''.join(a[i1:i2]))}</del>")
        if j2 > j1:
            out.append(f"<ins style='color:#1a7f37;font-weight:bold;text-decoration:none'>"
                       f"{html.escape(''.join(b[j1:j2]))}</ins>")
    return "".join(out)


def _change(c: dict) -> str:
    e = lambda k: html.escape(str(c.get(k, "")))
    old, new = str(c.get("old", "")), str(c.get("new", ""))
    if max(len(old), len(new)) > LONG_VALUE and old and new:
        value = _marked_diff(old, new)
    else:
        value = f"{e('old')} → {e('new')}"
    return (f"<li><b>{e('field')}</b>：{value}"
            f"<br><span style='color:#57606a'>理由：{e('reason') or '—'} · 来源：{_source(c.get('source'))}</span></li>")


def render_summary(*, applied: list[str], flagged: list[str], alert_only: bool,
                   details: dict[str, list[dict]] | None = None,
                   names: dict[str, str] | None = None) -> str:
    """Which funds changed AND what changed (field, old -> new, reason, source):
    the first version listed fund ids only, so the mail said nothing a person
    could check (10-10). Flagged entries carry apply_refresh's own reason."""
    details, names = details or {}, names or {}

    def li(items):
        return "".join(f"<li>{html.escape(x)}</li>" for x in items) or "<li>—</li>"

    def applied_li(fid):
        name = names.get(fid)
        head = f"{html.escape(name)}（{html.escape(fid)}）" if name else html.escape(fid)
        changes = "".join(_change(c) for c in details.get(fid, []))
        return f"<li>{head}" + (f"<ul>{changes}</ul>" if changes else "") + "</li>"
    banner = ('<p style="background:#fff3cd;padding:8px;border-radius:6px">'
              '⚠️ ALERT-ONLY 告警模式：未自动应用，请人工确认。</p>' if alert_only else "")
    if not applied and not flagged:
        body = "<p>本月无变化（no change）。</p>"
    else:
        applied_html = "".join(applied_li(f) for f in applied) or "<li>—</li>"
        body = (f"<h3>{'将会应用' if alert_only else '已自动应用'} ({len(applied)})</h3><ul>{applied_html}</ul>"
                f"<h3>转人工 ({len(flagged)})</h3><ul>{li(flagged)}</ul>")
    return (f"<html><head><meta charset='utf-8'><title>GMIA Profile Refresh</title></head>"
            f"<body style='font-family:sans-serif'>{banner}"
            f"<h2>GMIA 月度 Profile 刷新</h2>{body}</body></html>")


def send(subject: str, html_body: str) -> bool:
    host = os.environ.get("SMTP_HOST") or "smtp.163.com"; user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS"); to = os.environ.get("MAIL_TO", user)
    if not all([host, user, pw, to]):
        sys.stderr.write("[send_refresh_summary] SMTP env missing; skip\n"); return False
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
    msg["MIME-Version"] = "1.0"
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465")), timeout=30) as s:
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--applied", default=""); ap.add_argument("--flagged", default="")
    ap.add_argument("--alert-only", default="1")
    ap.add_argument("--drafts-dir", default="pending_profiles",
                    help="where the drafts are (applied/<id>.refresh.json after a real apply)")
    ap.add_argument("--sources", default="config/sources.json", help="for fund names")
    a = ap.parse_args()
    # Split on newline (not whitespace): flagged entries contain spaces, e.g.
    # "man-group (apply_refresh rc=1)".
    applied = [x.strip() for x in a.applied.split("\n") if x.strip()]
    flagged = [x.strip() for x in a.flagged.split("\n") if x.strip()]
    alert_only = a.alert_only == "1"
    html_body = render_summary(applied=applied, flagged=flagged, alert_only=alert_only,
                               details=load_details(applied, Path(a.drafts_dir)), names=load_names(Path(a.sources)))
    try:
        sent = send(f"GMIA Profile Refresh — {len(applied)} applied / {len(flagged)} flagged", html_body)
    except (OSError, smtplib.SMTPException) as e:
        sys.stderr.write(f"[send_refresh_summary] send failed: {type(e).__name__}: {e}\n")
        sent = False
    print(f"[send_refresh_summary] rendered ({len(applied)+len(flagged)} items), "
          f"alert_only={alert_only}, sent={sent}")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
