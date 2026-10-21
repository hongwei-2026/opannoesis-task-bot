#!/usr/bin/env python3
"""One-shot GitCode comment poller for GitHub Actions (no long-running loop).

Idempotency:
- Remember every comment id in .poll_state.json (seen).
- If state is missing/empty (cache miss), BOOTSTRAP: mark all current
  comments as seen and do NOT reply — never replay history as "new".
- Also skip comments older than watermarks.since (ISO time).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER = os.environ.get("GITCODE_OWNER", "OpenNoesis")
REPO = os.environ.get("GITCODE_REPO", "vllm")
API = os.environ.get("GITCODE_API", "https://api.gitcode.com/api/v5")
ROOT = Path(__file__).resolve().parents[2]
STATE = Path(os.environ.get("TASK_BOT_POLL_STATE", str(ROOT / ".poll_state.json")))
SCRIPT = ROOT / "team" / "scripts" / "task_bot.py"
TRIGGER_CI = ROOT / "team" / "scripts" / "trigger_ci.py"

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


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def interesting(body: str) -> bool:
    if not body:
        return False
    for line in body.splitlines():
        if CMD_LINE.match(line.strip()):
            return True
    return False


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


def bootstrap_if_needed(st, issues) -> bool:
    """If no durable cursor yet, mark everything seen and skip handling."""
    has_seen = bool(st.get("bootstrapped")) or any(st.get("seen") or {}.values())
    if has_seen:
        return False
    print("[poll] BOOTSTRAP: marking all existing comments as seen (no replay)", flush=True)
    for issue in issues:
        n = issue.get("number")
        if n is None:
            continue
        comments = list_comments(n)
        st.setdefault("seen", {})[str(n)] = [str(c.get("id")) for c in comments if c.get("id") is not None]
    st["bootstrapped"] = True
    st["since"] = utc_now()
    save_state(st)
    return True


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

    if bootstrap_if_needed(st, issues):
        print("[poll] bootstrap complete; wait for NEW comments next run", flush=True)
        return

    since = parse_ts(st.get("since"))
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
            # always remember id first — even if we skip handling
            seen.add(cid)

            created = parse_ts(c.get("created_at") or c.get("updated_at"))
            if since and created and created < since:
                # older than watermark but unseen id (e.g. pagination race): skip reply
                continue

            if interesting(c.get("body") or ""):
                run_comment(issue, c)
                handled += 1
        st["seen"][str(n)] = list(seen)[-500:]

    st["since"] = utc_now()
    st["bootstrapped"] = True
    save_state(st)
    print(f"[poll] done handled={handled}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"[poll] HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
