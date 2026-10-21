# OpenNoesis Task Bot（GitHub ↔ GitCode）

GitCode 组织流水线未开通时，用 **GitHub Actions** 定时轮询 `OpenNoesis/vllm` 的 Issue 评论并自动回复。

## 链路

```
用户在 GitCode Issue 发 CLAIM / APPROVE_CLAIM
        ↓
GitHub Actions 每 5 分钟扫一次 GitCode API
        ↓
用 secrets.GITCODE_TOKEN 在 GitCode 发评论
```

## 配置

仓库 Secrets 添加：

| Name | 说明 |
|------|------|
| `GITCODE_TOKEN` | GitCode PAT（需 issues 读写） |

## 命令（Issue 评论单独一行）

`CLAIM` · `APPROVE_CLAIM @id` · `ASSIGN @id` · `ABANDON` · `COMPLETE` · `SCORE`

## 手动触发

Actions → **gitcode-task-bot** → Run workflow
