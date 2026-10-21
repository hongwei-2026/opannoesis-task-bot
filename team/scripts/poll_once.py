#!/usr/bin/env python3
"""One-shot GitCode comment poller for GitHub Actions (no long-running loop)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = os.environ.get("GITCODE_OWNER", "OpenNoesis")
REPO = os.environ.get("GITCODE_REPO", "vllm")
API = os.environ.get("GITCODE_API", "https://api.gitcode.com/api/v5")
ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("TASK_BOT_POLL_STATE", str(ROOT / ".poll_state.json")))
SCRIPT = ROOT / "team" / "scripts" / "task_bot.py"

CMD_LINE = re.compile(
    r"^\s*(CLAIM|APPROVE_CLAIM|ASSIGN|REJECT_CLAIM|ABANDON|COMPLETE|SCORE|ACCEPT)"
    r"(?:\s+@?([A-Za-z0-9_.-]+))?(?:\s+(\d+(?:\.\d+)?))?\s*$",
    re.I,
)


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


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"seen": {}}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def interesting(body: str) -> bool:
    if not body:
        return False
    for line in body.splitlines():
        if CMD_LINE.match(line.strip()):
            return True
    return False


def run_comment(issue, comment):
    env = os.environ.copy()
    env.update(
        {
            "GITCODE_TOKEN": token(),
            "GITCODE_OWNER": OWNER,
            "GITCODE_REPO": REPO,
            "TASK_BOT_NO_GIT": "1",
            "TASK_BOT_MODE": "comment",
            "COMMENT_BODY": comment.get("body") or "",
            "COMMENT_USER": (comment.get("user") or {}).get("login") or "",
            "COMMENT_USER_TYPE": (comment.get("user") or {}).get("type") or "User",
            "ISSUE_NUMBER": str(issue.get("number")),
            "ISSUE_TITLE": issue.get("title") or "",
            "ISSUE_BODY": issue.get("body") or "",
        }
    )
    preview = (env["COMMENT_BODY"] or "").replace("\n", " ")[:80]
    print(f"[poll] #{issue['number']} @{env['COMMENT_USER']}: {preview!r}", flush=True)
    subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT), env=env, check=False)


def main():
    print(f"[poll] one-shot {OWNER}/{REPO}", flush=True)
    st = load_state()
    issues = api(f"/repos/{OWNER}/{REPO}/issues?state=open&per_page=50")
    if not isinstance(issues, list):
        issues = issues.get("data") or issues.get("list") or []
    handled = 0
    for issue in issues:
        n = issue.get("number")
        if n is None:
            continue
        comments = api(f"/repos/{OWNER}/{REPO}/issues/{n}/comments?per_page=100")
        if not isinstance(comments, list):
            comments = comments.get("data") or []
        seen = set(st["seen"].get(str(n), []))
        for c in comments:
            cid = str(c.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            if interesting(c.get("body") or ""):
                run_comment(issue, c)
                handled += 1
        st["seen"][str(n)] = list(seen)[-300:]
    save_state(st)
    print(f"[poll] done handled={handled}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"[poll] HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
