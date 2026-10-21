#!/usr/bin/env python3
"""Dispatch hongwei-2026/opannoesis-vllm-ci / vllm-gate and return run URL."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GH_API = "https://api.github.com"
CI_REPO = os.environ.get("CI_GITHUB_REPO", "hongwei-2026/opannoesis-vllm-ci")
WORKFLOW = os.environ.get("CI_WORKFLOW_FILE", "vllm-gate.yml")


def gh_token() -> str:
    t = os.environ.get("GH_DISPATCH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not t:
        raise SystemExit("GH_DISPATCH_TOKEN required")
    return t


def gh(method: str, path: str, body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{GH_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {gh_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "opannoesis-task-bot",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def gitcode_comment(issue: str, body: str) -> None:
    token = os.environ.get("GITCODE_TOKEN") or ""
    owner = os.environ.get("GITCODE_OWNER", "OpenNoesis")
    repo = os.environ.get("GITCODE_REPO", "vllm")
    if not token:
        return
    data = json.dumps({"body": body}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/{issue}/comments",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def dispatch(issue_number: str, ref: str = "main") -> str:
    before = gh("GET", f"/repos/{CI_REPO}/actions/runs?per_page=1")
    before_id = None
    runs = before.get("workflow_runs") or []
    if runs:
        before_id = runs[0].get("id")

    gh(
        "POST",
        f"/repos/{CI_REPO}/actions/workflows/{WORKFLOW}/dispatches",
        {
            "ref": "main",
            "inputs": {
                "ref": ref or "main",
                "issue_number": str(issue_number),
            },
        },
    )

    run_url = f"https://github.com/{CI_REPO}/actions/workflows/{WORKFLOW}"
    for _ in range(20):
        time.sleep(2)
        cur = gh("GET", f"/repos/{CI_REPO}/actions/runs?per_page=5")
        for run in cur.get("workflow_runs") or []:
            if run.get("name") != "vllm-gate" and "vllm-gate" not in (run.get("path") or ""):
                # still accept newest workflow_dispatch
                pass
            rid = run.get("id")
            if before_id and rid == before_id:
                continue
            if run.get("event") == "workflow_dispatch":
                return run.get("html_url") or run_url
    return run_url


def main():
    issue = os.environ.get("ISSUE_NUMBER") or ""
    login = os.environ.get("COMMENT_USER") or ""
    ref = os.environ.get("CI_REF") or "main"
    if not issue:
        raise SystemExit("ISSUE_NUMBER required")
    try:
        url = dispatch(issue, ref)
        gitcode_comment(
            issue,
            f"@{login} 已触发 CI（命令 `CI`）。\n查看运行：{url}\n"
            f"（开始/结束时还会再贴一次日志链接）",
        )
        print(f"dispatched {url}", flush=True)
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        gitcode_comment(issue, f"@{login} 触发 CI 失败：HTTP {e.code}\n```\n{err[:500]}\n```")
        raise


if __name__ == "__main__":
    main()
