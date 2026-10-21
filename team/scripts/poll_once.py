#!/usr/bin/env python3
"""One-shot GitCode comment poller for GitHub Actions.

Idempotency:
- Track comment ids in .poll_state.json.
- Cold start: mark comments older than LOOKBACK_HOURS as seen (no reply);
  still handle recent command comments so downtime does not drop CLAIMs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OWNER = os.environ.get("GITCODE_OWNER", "OpenNoesis")
REPO = os.environ.get("GITCODE_REPO", "vllm")
API = os.environ.get("GITCODE_API", "https://api.gitcode.com/api/v5")
ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("TASK_BOT_POLL_STATE", str(ROOT / ".poll_state.json")))
SCRIPT = ROOT / "team" / "scripts" / "task_bot.py"
TRIGGER_CI = ROOT / "team" / "scripts" / "trigger_ci.py"
LOOKBACK_HOURS = int(os.environ.get("TASK_BOT_LOOKBACK_HOURS", "12"))

CMD_LINE = re.compile(
    r"^\s*(CLAIM|APPROVE_CLAIM|ASSIGN|REJECT_CLAIM|ABANDON|COMPLETE|SCORE|ACCEPT|CI|RUN_CI)"
    r"(?:\s+@?([A-Za-z0-9_.-]+))?(?:\s+(\d+(?:\.\d+)?))?\s*$",
    re.I,
)
CI_LINE = re.compile(r"^\s*(CI|RUN_CI)(?:\s+(\S+))?\s*$", re.I)


def token() -> str:
    t = os.environ.get("GITCODE_TOKEN") or os.environ.get("TASK_BOT_TOKEN") or ""
    if not t:
        raise SystemExit("GITCODE_TOKEN required")
    return t


def api(path: str):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_s() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": {}, "bootstrapped": False, "since": None}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


BOT_MARKERS = (
    "已接取",
    "等待负责人",
    "已被",
    "待批准",
    "CI 已开始",
    "CI 结束",
    "已触发 CI",
    "不能**用中文",
    "负责人已把本任务交给你",
    "TASK-STATUS",
)


def interesting(body: str) -> bool:
    """Only pure short command comments — never bot prose that mentions CLAIM."""
    if not body or any(m in body for m in BOT_MARKERS):
        return False
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines or len(lines) > 3:
        return False
    # ignore fenced help blocks with many ticks
    if body.count("```") >= 2 and len(lines) > 1:
        return False
    return any(CMD_LINE.match(ln) for ln in lines)


def list_open_issues():
    issues = api(f"/repos/{OWNER}/{REPO}/issues?state=open&per_page=50")
    if not isinstance(issues, list):
        issues = issues.get("data") or issues.get("list") or []
    return issues


def list_comments(n: int):
    comments = api(f"/repos/{OWNER}/{REPO}/issues/{n}/comments?per_page=100")
    if not isinstance(comments, list):
        comments = comments.get("data") or []
    return comments


def run_comment(issue, comment):
    body = comment.get("body") or ""
    env = os.environ.copy()
    env.update(
        {
            "GITCODE_TOKEN": token(),
            "GITCODE_OWNER": OWNER,
            "GITCODE_REPO": REPO,
            "TASK_BOT_NO_GIT": "1",
            "TASK_BOT_MODE": "comment",
            "COMMENT_BODY": body,
            "COMMENT_USER": (comment.get("user") or {}).get("login") or "",
            "COMMENT_USER_TYPE": (comment.get("user") or {}).get("type") or "User",
            "ISSUE_NUMBER": str(issue.get("number")),
            "ISSUE_TITLE": issue.get("title") or "",
            "ISSUE_BODY": issue.get("body") or "",
        }
    )
    preview = body.replace("\n", " ")[:80]
    print(f"[poll] #{issue['number']} @{env['COMMENT_USER']}: {preview!r}", flush=True)

    for line in body.splitlines():
        m = CI_LINE.match(line.strip())
        if m:
            env["CI_REF"] = m.group(2) or "main"
            subprocess.run(
                [sys.executable, str(TRIGGER_CI)], cwd=str(ROOT), env=env, check=False
            )
            return

    subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT), env=env, check=False)


def main():
    print(f"[poll] one-shot {OWNER}/{REPO}", flush=True)
    st = load_state()
    issues = list_open_issues()
    cold = not (st.get("bootstrapped") or any((st.get("seen") or {}).values()))
    # Cold start: NEVER reply — only seed seen ids. Prevents history replay spam.
    if cold:
        print("[poll] COLD START seed-only (no replies)", flush=True)
        for issue in issues:
            n = issue.get("number")
            if n is None:
                continue
            comments = list_comments(n)
            st.setdefault("seen", {})[str(n)] = [
                str(c.get("id")) for c in comments if c.get("id") is not None
            ]
        st["since"] = utc_now_s()
        st["bootstrapped"] = True
        save_state(st)
        print("[poll] seed done; next run handles NEW comments only", flush=True)
        return

    bot_logins = {
        x.strip().lower()
        for x in (os.environ.get("TASK_BOT_LOGINS") or "hongwei-2026").split(",")
        if x.strip()
    }

    handled = 0
    for issue in issues:
        n = issue.get("number")
        if n is None:
            continue
        comments = list_comments(n)
        seen = set(st.setdefault("seen", {}).get(str(n), []))
        for c in comments:
            cid = str(c.get("id"))
            if cid in seen:
                continue
            seen.add(cid)

            login = ((c.get("user") or {}).get("login") or "").lower()
            body = c.get("body") or ""
            # never treat our own auto-replies as commands (even if they contain CLAIM text)
            if login in bot_logins and not (
                len([ln for ln in body.splitlines() if ln.strip()]) <= 2
                and interesting(body)
                and not any(m in body for m in BOT_MARKERS)
            ):
                # allow leader/bot account pure commands like APPROVE_CLAIM; skip prose
                if any(m in body for m in BOT_MARKERS) or len(body) > 120:
                    continue

            if interesting(body):
                run_comment(issue, c)
                handled += 1
        st["seen"][str(n)] = list(seen)[-500:]

    st["since"] = utc_now_s()
    st["bootstrapped"] = True
    save_state(st)
    print(f"[poll] done handled={handled}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"[poll] HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
