# PM Workplace 新人学习路线图

## 项目概览

PM Workplace 是 ABB 项目管理工具，核心功能是**从 Outlook 自动抓取合同相关邮件，用规则+本地AI自动识别项目所处的流程阶段**，以看板形式展示项目状态。

- **技术栈**: Python 3 后端 (stdlib HTTP Server) + 原生 JS 前端 + SQLite 数据库
- **核心依赖**: pywin32 (Outlook COM), openpyxl, PyMuPDF
- **仅支持 Windows** (传统 Outlook 客户端)


## 第一阶段：环境搭建与项目跑通（第 1-2 天）

### 学习目标
能把整个项目在本地跑起来，理解启动流程。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 1.1 | 安装 Python 依赖 | `pip install -r backend/requirements.txt` |
| 1.2 | 运行 `python start_pm_workplace.py` | 浏览器自动打开 `http://127.0.0.1:5050/` |
| 1.3 | 点击「同步 Outlook」 | 观察后端终端日志，理解邮件抓取流程 |
| 1.4 | 浏览看板界面 | 熟悉 7 个流程阶段列（销售开启合同 → … → 工厂反馈OA） |

### 关键代码文件
- `start_pm_workplace.py` — 启动入口，只有 7 行
- `backend/server.py` — HTTP 服务主程序，包含 REST API 路由
- `frontend/index.html` — SPA 入口页面

### 需要掌握的概念
- Python `http.server` 的基本用法（`BaseHTTPRequestHandler`）
- 该项目的 API 风格：`/api/health`、`/api/projects`、`/api/outlook/sync`
- 前后端分离但打包在一起的部署方式（后端直接 serve 前端静态文件）


## 第二阶段：数据层理解（第 3-4 天）

### 学习目标
理解数据如何存储、前端如何与后端通信。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 2.1 | 阅读 `backend/services/project_db.py` 前 150 行 | 理解 SQLite 表结构和 `PROJECT_COLUMNS` |
| 2.2 | 理解 Project 对象的完整字段 | contract, client, stage, stageDates, manualOverride 等 |
| 2.3 | 阅读 `frontend/js/api.js` | 理解前端如何调用后端 API（`fetch` / `XMLHttpRequest`）|
| 2.4 | 阅读 `frontend/js/data.js` | 理解 ORDER_STAGES 常量定义和前端数据模型 |

### 关键数据结构
```
Project {
  id, contract, name, client, amount, type, stage,
  stageDates, currentProgress, latestEmailTime,
  needsReview, reviewReason, manualOverride,
  aliases (项目别名系统), ...
}
```

### 需要掌握的概念
- **项目别名系统 (Aliases)**：一个项目可能有多种标识符（M4367-xxxx, CQxxxxx, 50xxxxx SO号, BT号），系统通过别名字段关联同一个项目的所有邮件
- **manualOverride**：人工修改过的字段在同步时不会被覆盖，但会标记 `needsReview`
- **stageDates**：记录各阶段的到达时间，用于前端看板展示


## 第三阶段：Outlook 同步核心逻辑（第 5-7 天）

### 学习目标
理解邮件抓取、解析、分类的完整流水线。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 3.1 | 阅读 `sync_outlook()` 函数 | 理解同步的整体流程（连接 → 遍历文件夹 → 解析 → 分类 → 合并 → 写入）|
| 3.2 | 阅读 `connect_outlook()` | 理解 Windows COM 初始化（`pythoncom.CoInitialize`）和 `gen_py` 缓存清理 |
| 3.3 | 阅读 `parse_email_item()` | 理解一封邮件提取哪些字段 |
| 3.4 | 阅读 `extract_contracts_with_type()` | 理解正则提取合同编号（M/K4367-xxxx, CQxxxxx, SO, BT） |
| 3.5 | 阅读 `get_items_in_date_range()` | 理解邮件按 `ReceivedTime` 降序排序和日期过滤 |

### 核心流水线（重要！）
```
Outlook 文件夹
  → get_items_in_date_range()    # 按日期范围过滤邮件
  → parse_email_item()           # 提取邮件元数据
  → extract_contracts_with_type() # 提取项目标识符
  → classify_email_progress()    # 规则分类邮件阶段
  → apply_llm_double_check()     # AI 复核（可选）
  → assign_project_objects()     # 将邮件归属到项目对象
  → resolve_current_progress()   # 确定每个项目的当前阶段
  → apply_sync_to_projects()     # 写入数据库
```

### 需要掌握的概念
- **COM 线程模型**：每个线程必须独立调用 `CoInitialize()`
- **邮件分类的 7 个标准流程阶段**（了解每个阶段的中英文含义和典型邮件特征）
- **候选邮件门控 (Candidate Gate)**：`strict_subject_evidence()` 函数决定了哪些邮件进入分类流水线


## 第四阶段：邮件分类系统（第 8-10 天）

### 学习目标
理解规则引擎 + AI 复核的双层分类架构。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 4.1 | 阅读 `strict_subject_evidence()` | 理解邮件证据评分（confirmed/review/ignore 三级）|
| 4.2 | 阅读 `classify_by_workflow_rules()` | 理解标准流程规则的优先级顺序 |
| 4.3 | 阅读 Excel Pattern 加载：`load_config_from_pattern_excel()` | 理解如何从 `Email Pattern11.xlsx` 读取评分规则 |
| 4.4 | 阅读 `score_progress()` | 理解基于关键词、附件数、图片的加权评分 |
| 4.5 | 阅读 `apply_llm_double_check()` | 理解 AI 复核的决策逻辑（confirm/review/ignore）|

### 分类规则优先级（从高到低）
```
工厂BT/OA回复 → PM开启BT09 → iProcess审批 → PA回复SO/BT09
→ Book订单申请 → 销售开启合同
```

### 需要掌握的概念
- **强规则 (Standard Rules)** 和 **评分规则 (Excel Pattern)** 的关系：强规则优先，评分规则作为兜底
- **AI 复核策略**：规则判定为 `needs_review` 的邮件才送 AI，规则明确的不浪费 AI 资源
- **阶段冲突处理**：规则和 AI 判断不一致时 → 人工审核


## 第五阶段：前端看板与交互（第 11-13 天）

### 学习目标
理解前端 SPA 架构和看板渲染逻辑。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 5.1 | 阅读 `frontend/js/app.js` 的 `render()` 方法 | 理解看板重新渲染的触发条件 |
| 5.2 | 阅读 Kanban 列渲染 | 理解项目卡片如何按 `stage` 分列展示 |
| 5.3 | 阅读项目编辑抽屉 | 理解表单提交如何触发 `PUT /api/projects/:id` |
| 5.4 | 阅读「同步 Outlook」弹窗 | 理解异步任务轮询机制 `GET /api/outlook/sync/:jobId` |
| 5.5 | 阅读 `frontend/js/sidebar.js` | 理解侧边栏导航和视图切换 |

### 前端 API 调用一览
| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/projects` | 获取所有项目 |
| POST | `/api/projects` | 创建项目 |
| PUT | `/api/projects/:id` | 更新项目 |
| DELETE | `/api/projects/:id` | 删除项目 |
| POST | `/api/outlook/sync` | 发起同步任务 |
| GET | `/api/outlook/sync/:jobId` | 查询同步进度 |
| POST | `/api/outlook/open-email` | 打开原始 Outlook 邮件 |
| POST | `/api/attachments/open` | 打开附件 |
| POST | `/api/contract-review` | 上传 PDF 合同审查 |

### 需要掌握的概念
- **事件驱动渲染**：`api.on("ordersUpdated", render)` 模式
- **同步任务状态机**：queued → running → completed/failed/cancelled
- **Undo 机制**：`/api/projects/snapshot` 实现前端操作回退


## 第六阶段：合同审查与附件管理（第 14-15 天）

### 学习目标
理解 PDF 上传、附件存储和合同审查流程。

### 具体任务
| # | 任务 | 说明 |
|---|------|------|
| 6.1 | 阅读 `backend/server.py` 的 `handle_contract_review()` | 理解 multipart 表单解析 |
| 6.2 | 阅读 `_parse_multipart_upload()` | 理解 boundary 分割和文件角色（contract/cqp/ta） |
| 6.3 | 阅读 `backend/services/contract_review.py` | 理解审查编排逻辑 |
| 6.4 | 阅读 `save_attachments()` | 理解附件目录结构和去重命名 |

### 需要掌握的概念
- **Multipart 表单解析**：手动解析 `Content-Type: multipart/form-data; boundary=...`
- **附件存储路径规则**：`attachments/{合同编号}/{日期}-{主题}-{EntryID后18位}/`
- **PDF 验证**：通过文件头 `%PDF-` 和大小限制（50MB）


## 第七阶段：实战练习（第 16-20 天）

### 小练习（由易到难）

| # | 练习 | 涉及模块 |
|---|------|---------|
| 7.1 | 在后端添加一个新 API `GET /api/stats`，返回各阶段项目数量统计 | server.py + project_db.py |
| 7.2 | 在前端看板顶部添加统计栏显示各阶段数量 | app.js + index.html |
| 7.3 | 新增一个邮件分类规则（如识别「发货通知」邮件类型） | outlook_sync.py |
| 7.4 | 给项目添加「标签」字段并支持按标签筛选 | project_db.py + server.py + app.js |
| 7.5 | 实现前端导出 CSV 功能（通过已有 API 获取数据，前端生成 CSV 并触发下载） | api.js + app.js |


## 关键设计理念（新人必须理解）

### 1. 人工优先于自动
- `manualOverride=true` 的字段**永不**被自动同步覆盖
- 自动分类与人工标记冲突时 → 标记 `needsReview`，不自动改写

### 2. 对象中心化合并
- 同一个项目的不同别名（CQ → M4367 → SO → BT）通过 `project_aliases` 表合并
- 使用 **Union-Find（并查集）** 算法连接同一项目的多个标识符

### 3. 同步幂等性
- 同一次同步中多次扫描同一项目不会创建重复记录
- `upsert_project()` 保证创建或更新语义
- 已有 stageDates 不会因重新同步而丢失早期阶段日期

### 4. 渐进式增强
- 旧同步数据没有 `latestEmailEntryId` → 回退到 `get_latest_email_for_contract()`
- 规则分类能力不足 → 降级到 Excel Pattern → 再降级到人工审核

### 5. 仅 Windows 平台
- Outlook COM 自动化需要 `pywin32`，**非 Windows 环境无法运行同步功能**
- 前端页面在其他系统可以打开，但核心同步功能不可用


## 学习检查清单

- [ ] 能在本地跑起项目并完成一次 Outlook 同步
- [ ] 能解释 7 个流程阶段分别代表什么业务含义
- [ ] 能追踪一封邮件从 Outlook → 解析 → 分类 → 入库的完整链路
- [ ] 能解释项目别名（alias）系统如何工作，为什么需要它
- [ ] 能解释 manualOverride 机制及其安全策略
- [ ] 能理解 AI 复核只在规则不确定时触发，不会对所有邮件调用
- [ ] 能在前端添加一个新按钮并连接到后端新 API
- [ ] 能在后端添加一个新的邮件分类规则
- [ ] 理解同步任务的异步架构（threading + cancel event）
- [ ] 理解附件存储和去重策略