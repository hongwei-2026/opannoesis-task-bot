#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenNoesis/vllm 任务机器人（移植自 minda_mianshi 无人机仓 team-task-bot）。
- 评论区可见回复（post_comment）
- 同步维护 Issue 描述中的状态段
- GitCode API v5
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEAM = ROOT / "team"
CONFIG_PATH = TEAM / "config.json"
STATE_PATH = TEAM / "state.json"
LEADERBOARD_PATH = TEAM / "leaderboard.md"

STATUS_START = "<!-- TEAM-TASK-STATUS:START -->"
STATUS_END = "<!-- TEAM-TASK-STATUS:END -->"

# 仅整条评论几乎只有「认领」类短句才算中文误触发；正文里顺带出现不算
CN_CLAIM_RE = re.compile(
    r"^\s*(认领|接取|领取|我要做|我来做|我认领|想接这个)\s*[。.!！]?\s*$",
    re.I,
)
CLAIM_HELP = "请单独发一行英文命令 `CLAIM`（中文无效）。"

CLAIMED_TAG_RE = re.compile(r"\s*\[claimed:@([A-Za-z0-9_-]+)\]\s*", re.I)
CMD_RE = re.compile(
    r"^\s*(CLAIM|APPROVE_CLAIM|ASSIGN|REJECT_CLAIM|ABANDON|COMPLETE|SCORE|ACCEPT)"
    r"(?:\s+@?([A-Za-z0-9_.-]+))?(?:\s+(\d+))?\s*$",
    re.I | re.M,
)
URL_RE = re.compile(r"https?://\S+", re.I)
PR_URL_RE = re.compile(r"/pulls?/(\d+)|/merge_requests?/(\d+)", re.I)

STATUS_LABEL = {
    "open": "开放（可 CLAIM）",
    "pending_claim": "已接取，等待负责人批准",
    "assigned": "已归属（开发中）",
    "pending_accept": "已提交，等待负责人计分",
    "awarded": "已验收并计分",
}
ACTIVE_HOLD = frozenset({"pending_claim", "assigned", "pending_accept"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api(method: str, path: str, payload=None, form=False):
    token = (
        os.environ.get("GITCODE_TOKEN")
        or os.environ.get("TASK_BOT_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("ATOMGIT_TOKEN")
    )
    if not token:
        raise SystemExit("GITCODE_TOKEN / TASK_BOT_TOKEN missing")
    owner = os.environ.get("GITCODE_OWNER", "OpenNoesis")
    repo = os.environ.get("GITCODE_REPO", "vllm")
    base = os.environ.get("GITCODE_API", "https://api.gitcode.com/api/v5")
    url = f"{base}{path}"
    if "{owner}" in url:
        url = url.replace("{owner}", owner).replace("{repo}", repo)
    headers = {
        "Authorization": f"Bearer {token}",
        "PRIVATE-TOKEN": token,
        "Accept": "application/json",
    }
    data = None
    if payload is not None:
        if form:
            # GitCode issue PATCH often wants multipart/form
            boundary = "----TaskBotBoundary7MA4YWxkTrZu0gW"
            parts = []
            for k, v in payload.items():
                if v is None:
                    continue
                parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                    f"{v}\r\n"
                )
            parts.append(f"--{boundary}--\r\n")
            data = "".join(parts).encode("utf-8")
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        print("API_ERROR", method, url, e.code, err, file=sys.stderr)
        raise


def get_issue(n: int) -> dict:
    owner = os.environ.get("GITCODE_OWNER", "OpenNoesis")
    repo = os.environ.get("GITCODE_REPO", "vllm")
    return api("GET", f"/repos/{owner}/{repo}/issues/{n}")


def patch_issue(n: int, *, title=None, body=None, state=None) -> None:
    owner = os.environ.get("GITCODE_OWNER", "OpenNoesis")
    # GitCode: PATCH /repos/{owner}/issues/{number} with form repo=
    repo = os.environ.get("GITCODE_REPO", "vllm")
    form = {"repo": repo}
    if title is not None:
        form["title"] = title
    if body is not None:
        form["body"] = body
    if state is not None:
        form["state"] = state
    api("PATCH", f"/repos/{owner}/issues/{n}", form, form=True)


def post_comment(n: int, body: str) -> None:
    owner = os.environ.get("GITCODE_OWNER", "OpenNoesis")
    repo = os.environ.get("GITCODE_REPO", "vllm")
    api("POST", f"/repos/{owner}/{repo}/issues/{n}/comments", {"body": body})


def looks_like_chinese_claim(text: str) -> bool:
    if not text:
        return False
    return bool(CN_CLAIM_RE.match(text.strip()))


def is_leader(login: str, config: dict) -> bool:
    return login.lower() in {x.lower() for x in config.get("leaders", [])}


def parse_command(body: str):
    if not body:
        return None, None, None
    # 兼容：APPROVE_CLAIM [@user](https://gitcode.com/user)
    text = re.sub(
        r"\[@([A-Za-z0-9_.-]+)\]\([^)]+\)",
        r"@\1",
        body or "",
    )
    text = re.sub(r"https?://gitcode\.com/([A-Za-z0-9_.-]+)\b", r"@\1", text)
    for line in text.splitlines():
        m = CMD_RE.match(line.strip())
        if m:
            return m.group(1).upper(), (m.group(2) or "").strip() or None, m.group(3)
    return None, None, None


def extract_pr(body: str) -> str | None:
    text = body or ""
    for line in text.splitlines():
        if re.match(r"^\s*PR\s*:", line, re.I):
            m = URL_RE.search(line)
            if m:
                return m.group(0).rstrip(").,]")
    urls = URL_RE.findall(text)
    for u in urls:
        if PR_URL_RE.search(u) or "/merge_requests/" in u:
            return u.rstrip(").,]")
    return urls[0].rstrip(").,]") if urls else None


def set_title_claimed(title: str, login: str | None) -> str:
    base = CLAIMED_TAG_RE.sub("", title).rstrip()
    if not login:
        return base
    return f"{base} [claimed:@{login}]"


def sync_task_from_title(title: str, task: dict) -> None:
    """Title [claimed:@user] is source of truth when state.json lagged."""
    m = CLAIMED_TAG_RE.search(title or "")
    if not m:
        return
    holder = m.group(1)
    task["assignee"] = holder
    task["pending_claimer"] = None
    if task.get("status") in (None, "open", "pending_claim"):
        task["status"] = "assigned"

def upsert_block(body: str, start: str, end: str, block: str) -> str:
    body = body or ""
    if start in body and end in body:
        pre, rest = body.split(start, 1)
        _, post = rest.split(end, 1)
        return pre.rstrip() + "\n\n" + block + "\n" + post.lstrip("\n")
    if body.strip():
        return body.rstrip() + "\n\n" + block + "\n"
    return block + "\n"


def render_status_panel(task: dict, message: str, leader_name: str) -> str:
    st = task.get("status") or "open"
    holder = task.get("assignee")
    pending = task.get("pending_claimer")
    return "\n".join(
        [
            STATUS_START,
            "## 任务状态（机器人自动维护 · 请勿手改本段）",
            "",
            "| 字段 | 值 |",
            "|------|-----|",
            f"| 状态 | {STATUS_LABEL.get(st, st)} (`{st}`) |",
            f"| 接取人 | {('@' + holder) if holder else '—'} |",
            f"| 待批申请人 | {('@' + pending) if pending else '—'} |",
            f"| 分值 | **{task.get('score', 10)}** |",
            f"| PR | {task.get('pr') or '—'} |",
            f"| 最近活动 | {task.get('last_activity') or utc_now()} |",
            f"| 最近更新 | {utc_now()} |",
            "",
            f"**最近消息：** {message}",
            "",
            "命令（英文，单独一行）：`CLAIM` · `APPROVE_CLAIM` / `ASSIGN @id` · `ABANDON` · "
            f"`COMPLETE` · `SCORE <分>`（{leader_name}）",
            STATUS_END,
        ]
    )


def write_issue_status(n, current_body, task, message, leader_name, *, title=None):
    panel = render_status_panel(task, message, leader_name)
    new_body = upsert_block(current_body or "", STATUS_START, STATUS_END, panel)
    fields = {"body": new_body}
    if title is not None:
        fields["title"] = title
    patch_issue(n, **fields)


def user_holds(task: dict, login: str) -> bool:
    l = (login or "").lower()
    return (task.get("assignee") or "").lower() == l or (task.get("pending_claimer") or "").lower() == l


def find_blockers(state: dict, login: str, exclude: int | None = None):
    out = []
    for key, task in (state.get("tasks") or {}).items():
        try:
            n = int(key)
        except ValueError:
            continue
        if exclude is not None and n == exclude:
            continue
        if not isinstance(task, dict) or not user_holds(task, login):
            continue
        st = task.get("status")
        if st in ACTIVE_HOLD:
            out.append((n, task, STATUS_LABEL.get(st, st)))
        elif st == "awarded":
            # still holding if issue not closed — best-effort skip
            pass
    return out


def render_leaderboard(state: dict) -> str:
    scores: dict[str, dict] = {}
    details = []
    progress = []
    for iid, t in sorted((state.get("tasks") or {}).items(), key=lambda x: int(x[0])):
        st = t.get("status")
        holder = t.get("assignee")
        pts = int(t.get("score") or 0)
        if st == "awarded" and holder:
            scores.setdefault(holder, {"points": 0, "count": 0})
            scores[holder]["points"] += pts
            scores[holder]["count"] += 1
            details.append(f"- @{holder} · #{iid} · **+{pts}**")
        elif st in ("assigned", "pending_accept") and holder:
            progress.append(f"- @{holder} · #{iid} · `{st}` · 分值 {pts}")
        elif st == "pending_claim" and t.get("pending_claimer"):
            progress.append(f"- @{t['pending_claimer']} · #{iid} · 待批准 · 分值 {pts}")
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1]["points"], kv[0].lower()))
    lines = [
        "# OpenNoesis/vllm 任务计分板",
        "",
        "> 由任务机器人根据负责人 `SCORE` / `ACCEPT` 自动更新。",
        "",
        "| 排名 | GitCode ID | 总分 | 已验收任务数 |",
        "|------|------------|------|--------------|",
    ]
    if not ranked:
        lines.append("| — | — | 0 | 0 |")
    else:
        for i, (user, info) in enumerate(ranked, 1):
            lines.append(f"| {i} | @{user} | {info['points']} | {info['count']} |")
    lines += ["", "## 明细", "", "\n".join(details) if details else "（暂无）"]
    lines += ["", "## 进行中", "", "\n".join(progress) if progress else "（暂无）", ""]
    return "\n".join(lines)


def git_commit_push(paths: list[Path], message: str) -> None:
    if os.environ.get("TASK_BOT_NO_GIT") == "1":
        return
    try:
        subprocess.run(["git", "config", "user.name", "opennoesis-task-bot"], check=False)
        subprocess.run(
            ["git", "config", "user.email", "bot@opennoesis.local"],
            check=False,
        )
        for p in paths:
            subprocess.run(["git", "add", str(p.relative_to(ROOT))], check=False, cwd=ROOT)
        st = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if st.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], check=True, cwd=ROOT)
        branch = os.environ.get("TASK_BOT_PUSH_BRANCH", "main")
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=True, cwd=ROOT)
    except Exception as e:
        print("GIT_PUSH_WARN", e, file=sys.stderr)


def touch_activity(task: dict) -> None:
    task["last_activity"] = utc_now()


def handle_comment_event(event: dict) -> None:
    comment_body = (event.get("comment") or {}).get("body") or ""
    login = (event.get("comment") or {}).get("user", {}).get("login") or ""
    issue = event.get("issue") or {}
    issue_n = int(issue["number"])
    user_type = (event.get("comment") or {}).get("user", {}).get("type") or ""
    if user_type.lower() == "bot":
        return

    config = load_json(CONFIG_PATH, {})
    if issue_n in set(config.get("skip_claim_on_issues") or []):
        # still guide chinese claim on parent
        if looks_like_chinese_claim(comment_body) or parse_command(comment_body)[0] == "CLAIM":
            post_comment(
                issue_n,
                f"@{login} 这是**总览 Issue**，请到具体模型子任务 Issue 下发送 `CLAIM`。",
            )
        return

    cmd, arg, score_arg = parse_command(comment_body)
    if not cmd:
        # 负责人闲聊不回；中文误触只回一行，且须整评几乎只有「认领」
        if is_leader(login, config):
            print("ignore leader non-command")
            return
        if looks_like_chinese_claim(comment_body):
            post_comment(issue_n, f"@{login} {CLAIM_HELP}")
            return
        print("no command")
        return

    state = load_json(STATE_PATH, {"tasks": {}})
    key = str(issue_n)
    task = state.setdefault("tasks", {}).setdefault(
        key,
        {
            "status": "open",
            "assignee": None,
            "pending_claimer": None,
            "score": int(config.get("score_defaults", {}).get("default", 10)),
            "pr": None,
            "last_activity": utc_now(),
        },
    )
    title = issue.get("title") or ""
    body = issue.get("body") or ""
    leader_name = config.get("leader_display_name", "负责人")
    sync_task_from_title(title, task)

    def persist(msg: str):
        state["updated_at"] = utc_now()
        LEADERBOARD_PATH.write_text(render_leaderboard(state), encoding="utf-8")
        save_json(STATE_PATH, state)
        git_commit_push([STATE_PATH, LEADERBOARD_PATH], msg)

    def reply(message: str, *, new_title=None):
        cur = get_issue(issue_n)
        write_issue_status(
            issue_n,
            cur.get("body") or body,
            task,
            message,
            leader_name,
            title=new_title,
        )
        post_comment(issue_n, f"@{login} {message}")

    touch_activity(task)

    if cmd == "CLAIM":
        # 负责人用 CLAIM 无意义，静默（避免刷屏）
        if is_leader(login, config):
            print("ignore leader CLAIM")
            return
        holder = (task.get("assignee") or "").lower()
        pending = (task.get("pending_claimer") or "").lower()
        me = login.lower()
        if task.get("status") in ("assigned", "pending_accept", "awarded"):
            if holder == me:
                print("ignore self re-CLAIM while holding")
                return
            reply(
                f"这个任务已被 `@{task.get('assignee')}` 接取，请换其他开放任务。"
            )
            return
        if task.get("status") == "pending_claim" and pending:
            if pending == me:
                print("ignore duplicate pending CLAIM")
                return
            reply(
                f"这个任务已被 `@{task.get('pending_claimer')}` 申请，正在等批准，请换其他任务。"
            )
            return
        blockers = find_blockers(state, login, exclude=issue_n)
        if blockers:
            ids = ", ".join(f"#{n}" for n, _, _ in blockers)
            reply(f"你还有未完成任务（{ids}），先 `COMPLETE` 或 `ABANDON` 再 CLAIM。")
            return
        task["status"] = "pending_claim"
        task["pending_claimer"] = login
        persist(f"bot: CLAIM #{issue_n} by @{login}")
        reply(f"已记录接取，请负责人 `APPROVE_CLAIM` 或 `ASSIGN @{login}`。")
        return

    if cmd in ("APPROVE_CLAIM", "ASSIGN"):
        if not is_leader(login, config):
            reply(f"仅负责人（{leader_name}）可 `{cmd}`。")
            return
        target = arg or task.get("pending_claimer")
        if not target:
            reply("没有指定接取人。用法：`APPROVE_CLAIM @用户` 或先让队员 `CLAIM`。")
            return
        # strip @ if present
        target = target.lstrip("@")
        blockers = find_blockers(state, target, exclude=issue_n)
        if blockers:
            reply(f"**不能批准。** `@{target}` 仍有未完成任务（一人一任务）。")
            return
        task["status"] = "assigned"
        task["assignee"] = target
        task["pending_claimer"] = None
        persist(f"bot: {cmd} #{issue_n} -> @{target}")
        reply(
            f"已交给 `@{target}`（正式接取）。其他人请换任务。完成后发：\n```text\nCOMPLETE\nPR: <链接>\n```",
            new_title=set_title_claimed(title, target),
        )
        return

    if cmd == "REJECT_CLAIM":
        if not is_leader(login, config):
            reply(f"仅负责人可 `REJECT_CLAIM`。")
            return
        pending = task.get("pending_claimer")
        task["status"] = "open"
        task["pending_claimer"] = None
        persist(f"bot: REJECT_CLAIM #{issue_n}")
        reply("已驳回待批认领" + (f"（`@{pending}`）" if pending else "") + "。他人可重新 `CLAIM`。")
        return

    if cmd == "ABANDON":
        holder = task.get("assignee")
        pending = task.get("pending_claimer")
        allowed = is_leader(login, config)
        if holder and holder.lower() == login.lower():
            allowed = True
        if pending and pending.lower() == login.lower():
            allowed = True
        if not allowed:
            reply("只有当前申请人/持有者或负责人可以 `ABANDON`。")
            return
        task["status"] = "open"
        task["assignee"] = None
        task["pending_claimer"] = None
        task["pr"] = None
        persist(f"bot: ABANDON #{issue_n} by @{login}")
        reply("已放弃任务，状态重回开放，其他人可以 `CLAIM`。", new_title=set_title_claimed(title, None))
        return

    if cmd == "COMPLETE":
        if not task.get("assignee") or task["assignee"].lower() != login.lower():
            # 负责人误发 COMPLETE：静默；其他人一句说明
            if is_leader(login, config):
                print("ignore leader COMPLETE")
                return
            reply("需先成为任务持有者后再 `COMPLETE`，并附上 `PR: <链接>`。")
            return
        pr = extract_pr(comment_body)
        if not pr:
            reply("请带上实现 PR，例如：\n```text\nCOMPLETE\nPR: https://gitcode.com/.../pulls/N\n```")
            return
        task["status"] = "pending_accept"
        task["pr"] = pr
        persist(f"bot: COMPLETE #{issue_n}")
        reply(f"已提交完成，PR：{pr}。负责人可用 `SCORE` / `ACCEPT` 验收。")

        return

    if cmd in ("SCORE", "ACCEPT"):
        if not is_leader(login, config):
            reply(f"仅负责人可 `{cmd}`。")
            return
        if task.get("status") != "pending_accept" and cmd == "SCORE" and not task.get("assignee"):
            reply("当前不在待验收状态。持有者需先 `COMPLETE`。")
            return
        if score_arg:
            task["score"] = int(score_arg)
        if task.get("status") not in ("pending_accept", "assigned", "awarded"):
            # allow SCORE after assign if leader insists
            if not task.get("assignee"):
                reply("没有持有者，无法计分。")
                return
        task["status"] = "awarded"
        task["awarded_at"] = utc_now()
        task["awarded_by"] = login
        persist(f"bot: SCORE #{issue_n} +{task['score']} @{task.get('assignee')}")
        reply(
            f"**验收通过并计分。** 持有者 `@{task.get('assignee')}` 得分 **+{task['score']}**，"
            f"已写入 `team/leaderboard.md`。"
        )
        try:
            patch_issue(issue_n, state="close")
        except Exception as e:
            print("CLOSE_WARN", e, file=sys.stderr)
        return

    reply(f"未知命令 `{cmd}`。\n\n{CLAIM_HELP}")


def handle_assigned_event(event: dict) -> None:
    """网页指派 Assignee 时也要说话。"""
    issue = event.get("issue") or {}
    issue_n = int(issue["number"])
    assignee = (event.get("assignee") or issue.get("assignee") or {}).get("login")
    if not assignee and isinstance(issue.get("assignees"), list) and issue["assignees"]:
        assignee = issue["assignees"][0].get("login")
    actor = (event.get("sender") or event.get("user") or {}).get("login") or "maintainer"
    if not assignee:
        print("assign without assignee")
        return
    config = load_json(CONFIG_PATH, {})
    if issue_n in set(config.get("skip_claim_on_issues") or []):
        return
    state = load_json(STATE_PATH, {"tasks": {}})
    key = str(issue_n)
    task = state.setdefault("tasks", {}).setdefault(
        key,
        {
            "status": "open",
            "assignee": None,
            "pending_claimer": None,
            "score": int(config.get("score_defaults", {}).get("default", 10)),
            "pr": None,
            "last_activity": utc_now(),
        },
    )
    task["status"] = "assigned"
    task["assignee"] = assignee
    task["pending_claimer"] = None
    touch_activity(task)
    state["updated_at"] = utc_now()
    LEADERBOARD_PATH.write_text(render_leaderboard(state), encoding="utf-8")
    save_json(STATE_PATH, state)
    git_commit_push([STATE_PATH, LEADERBOARD_PATH], f"bot: UI assign #{issue_n} -> @{assignee}")
    title = set_title_claimed(issue.get("title") or "", assignee)
    cur = get_issue(issue_n)
    write_issue_status(
        issue_n,
        cur.get("body") or "",
        task,
        f"负责人已通过网页指派本任务给 @{assignee}",
        config.get("leader_display_name", "负责人"),
        title=title,
    )
    post_comment(
        issue_n,
        f"@{assignee} **负责人（`@{actor}`）已把本任务交给你。**\n\n"
        f"分值 **{task['score']}**。请开 Design PR，并把 Design PR + Issue + GitCode ID "
        f"发到 `feizi_050920@qq.com`，然后实现并评论：\n"
        f"```text\nCOMPLETE\nPR: <实现PR链接>\n```\n"
        f"放弃请评论 `ABANDON`。",
    )


def handle_stale_release() -> None:
    config = load_json(CONFIG_PATH, {})
    days = int(config.get("stale_days", 14))
    state = load_json(STATE_PATH, {"tasks": {}})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    changed = False
    for key, task in list((state.get("tasks") or {}).items()):
        if not isinstance(task, dict):
            continue
        if task.get("status") not in ACTIVE_HOLD:
            continue
        last = task.get("last_activity") or task.get("awarded_at")
        if not last:
            continue
        try:
            ts = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts > cutoff:
            continue
        n = int(key)
        holder = task.get("assignee") or task.get("pending_claimer") or "unknown"
        task["status"] = "open"
        task["assignee"] = None
        task["pending_claimer"] = None
        task["pr"] = None
        touch_activity(task)
        changed = True
        try:
            cur = get_issue(n)
            write_issue_status(
                n,
                cur.get("body") or "",
                task,
                f"超过 {days} 天无进展，机器人已自动释放（原持有者 @{holder}）",
                config.get("leader_display_name", "负责人"),
                title=set_title_claimed(cur.get("title") or "", None),
            )
            post_comment(
                n,
                f"**自动释放：** @{holder} 接取后超过 **{days}** 天无明显进展，任务已重新开放，他人可 `CLAIM`。",
            )
        except Exception as e:
            print("STALE_WARN", n, e, file=sys.stderr)
    if changed:
        state["updated_at"] = utc_now()
        LEADERBOARD_PATH.write_text(render_leaderboard(state), encoding="utf-8")
        save_json(STATE_PATH, state)
        git_commit_push([STATE_PATH, LEADERBOARD_PATH], f"bot: stale release after {days}d")


def main() -> None:
    mode = os.environ.get("TASK_BOT_MODE", "comment")
    if mode == "stale":
        handle_stale_release()
        return
    if mode == "assigned":
        event_path = os.environ.get("GITHUB_EVENT_PATH") or os.environ.get("ATOMGIT_EVENT_PATH")
        if not event_path:
            # build from env
            event = {
                "issue": {
                    "number": int(os.environ["ISSUE_NUMBER"]),
                    "title": os.environ.get("ISSUE_TITLE", ""),
                    "body": "",
                    "assignee": {"login": os.environ.get("ASSIGNEE_LOGIN", "")},
                },
                "assignee": {"login": os.environ.get("ASSIGNEE_LOGIN", "")},
                "sender": {"login": os.environ.get("ACTOR_LOGIN", "")},
            }
        else:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        handle_assigned_event(event)
        return

    # Prefer synthetic env event when COMMENT_BODY/ISSUE_NUMBER set.
    # On GitHub Actions, GITHUB_EVENT_PATH is always present (push/schedule) and
    # must NOT override the poller-injected comment payload.
    use_env = os.environ.get("COMMENT_BODY") is not None and os.environ.get("ISSUE_NUMBER")
    event_path = os.environ.get("ATOMGIT_EVENT_PATH") or (
        None if use_env else os.environ.get("GITHUB_EVENT_PATH")
    )
    if event_path and Path(event_path).exists() and not use_env:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    else:
        event = {
            "action": "created",
            "comment": {
                "body": os.environ.get("COMMENT_BODY", ""),
                "user": {
                    "login": os.environ.get("COMMENT_USER", ""),
                    "type": os.environ.get("COMMENT_USER_TYPE", "User"),
                },
            },
            "issue": {
                "number": int(os.environ["ISSUE_NUMBER"]),
                "title": os.environ.get("ISSUE_TITLE", ""),
                "body": os.environ.get("ISSUE_BODY", ""),
            },
        }
    if event.get("action") and event.get("action") not in ("created",):
        print("skip action", event.get("action"))
        return
    handle_comment_event(event)


if __name__ == "__main__":
    main()
