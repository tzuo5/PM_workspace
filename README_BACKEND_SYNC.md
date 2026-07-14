# PM Workplace Outlook Sync Backend

## Run

```bash
pip install -r backend/requirements.txt
python start_pm_workplace.py
```

The browser opens `http://127.0.0.1:5050/`. The backend does **not** connect to Outlook during startup. Outlook is touched only after clicking **同步 Outlook** in the front end.

## Sync input format

- 邮箱账号: Outlook root mailbox display name or email address, for example `tianhao.zuo@cn.abb.com`
- 文件夹路径: path from the mailbox root, for example `Inbox/OA`
- 日期范围: defaults to local-machine today minus 7 days through today
- 包含子文件夹: enabled by default

If the log says a folder has items but reads zero mails, the date range does not cover any messages in that folder. Expand the start/end dates first before changing the folder path.

## Persistence

Data is stored in SQLite:

```text
backend/data/pm_tracker.db
backend/data/attachments/
```

Manual edits are persisted with `manualOverride=true`. During future Outlook syncs, manual fields and manually set stage are preserved; conflicting or backward email progress is marked as `需确认`.

## Standard workflow rules

The sync now uses deterministic standard-order rules before falling back to the Excel scoring model:

| Mail evidence | Front-end stage |
|---|---|
| Sales starts contract process, e.g. `申请开通...合同...流程` | 销售开启合同 |
| PM asks PA to create BT09, e.g. `请帮忙建BT09订单` | PM开启BT09 |
| PA replies with BT + SO numbers, e.g. `BTC014695 505317866` | PA回复SO/BT09 |
| SA sends iProcess link / QueueID | iProcess审批 |
| SA submits `CHECK-...` or `Please check and book in SAP`; PM confirms prepayment | Book订单申请 |
| After PM asks PA to place order, PA replies with factory BT number | 工厂BT回复 |
| Future OA / Order Acknowledgement messages | 工厂反馈OA |

## Legacy Email Pattern mapping

| Email Pattern progress | Front-end stage |
|---|---|
| 销售合同已开启 | 销售开启合同 |
| BT09待创建 | PM开启BT09 |
| BT09邮件已发送 | PA回复SO/BT09 |
| iprocess已上传 | iProcess审批 |
| 预付款已到 / 合同待Aimee Book | Book订单申请 |
| 合同已批准（待下单） / 已下单给工厂 | 工厂BT回复 |
| RFC已完成 / OA已反馈 / Your ABB Order | 工厂反馈OA |

## Frontend batch operation additions

This version also adds three front-end workflow controls:

- **删除全部**: opens a confirmation drawer with a select-all checkbox and one checkbox per workflow stage. The deletion scope follows the current tab, current search keyword, and visible columns in column view.
- **返回**: restores the project table to the state before the last front-end operation. The undo snapshot is also written back to SQLite through `/api/projects/snapshot`.
- **Compact event cards**: Kanban cards are compact by default and show only the project number and company name. Hover or keyboard focus expands the full card.

## Open original Outlook mail

Project edit drawers now include **查看原始邮件** for Outlook-synced projects. Clicking it calls:

```text
POST /api/outlook/open-email
```

The backend uses the stored Outlook `EntryID` and `StoreID` to open the exact MailItem in the classic Outlook client. Existing rows created before this version may not have those IDs; re-run Outlook sync for those projects if the button cannot locate the source mail.


## Backend cleanup

Legacy experimental folders were removed from the packaged version. The active backend surface is now limited to `backend/server.py`, `backend/services/`, `backend/config/Email Pattern11.xlsx`, and runtime-generated `backend/data/`.

## Local AI double-check

This version supports LM Studio local AI review only for rule-level manual-review candidates. Rule-confirmed messages are no longer sent to the model, which keeps sync speed usable on small local models.

Recommended LM Studio settings:

```text
Model: qwen/qwen3-0.6b
Server: http://127.0.0.1:1234
API mode: OpenAI-compatible
Endpoint used by backend: http://127.0.0.1:1234/v1/chat/completions
```

Configuration file:

```text
backend/config/llm_config.json
```

Default config:

```json
{
  "enabled": true,
  "provider": "lmstudio",
  "base_url": "http://127.0.0.1:1234/v1",
  "model": "qwen/qwen3-0.6b",
  "temperature": 0,
  "max_tokens": 500,
  "timeout_seconds": 60,
  "confirm_threshold": 0.75,
  "review_threshold": 0.45
}
```

The Outlook sync modal includes **使用本地 AI 复核人工审核候选**. When enabled, the backend flow is:

```text
Outlook full scan
→ strict subject/thread coarse filter
→ deterministic rule classification for clear messages
→ local AI review only for rule-level manual-review candidates
→ confirmed / review / ignored
→ write only confirmed + review rows into SQLite
```

Safety policy:

- AI `confirmed` with confidence >= 0.75 can enter the normal workflow columns.
- AI `review`, low confidence, JSON parsing failure, or rule/AI stage conflict goes to **需要人工审核**.
- AI `ignored` is not written to the project table.
- If a RE/FW workflow email is found but the original sales-start email is not found in the scan window, the item goes to **需要人工审核** even if AI thinks it is relevant.
- Cancelled sync jobs roll back the current run and delete attachments saved by that run.

## Project object / alias tracking update

This package now treats every tracked order as one project object instead of one row per identifier.

New backend tables are added automatically on startup:

```text
project_aliases   CQ / M4367 / SO / BT / FLOW identifiers mapped to one project id
project_events    Outlook evidence timeline used to classify project progress
manual_overrides  reserved audit table for human edits
```

The existing `/api/projects` response shape is unchanged, so the current front end continues to use `id`, `contract`, `client`, `stage`, `latestEmailTime`, and related fields as before.

Important behavior changes:

- A CQ-only opening mail such as `请开启迅亚CQ1106414合同流程` can create the first project object.
- A later mail such as `check-迅亚CQ1106414-M4367-3569-...` is merged into the same object instead of creating a separate M4367 row.
- `CQ1106414`, `M4367-3569`, `505368900`, and BT numbers are stored as aliases for later matching.
- The visible `contract` field can upgrade from CQ to M/K when the stronger identifier becomes available, while the internal project id remains stable.
- Conflicting aliases that point to multiple existing projects are not auto-merged; the project is marked as needing manual review.
