# 附件展示功能说明

本次修改不改变邮件分类逻辑，只新增“项目附件读取与打开”能力。

## 新增能力

- 点击前端项目卡片 / 列表行后，项目侧边弹窗中会显示“邮件附件”。
- 附件来自当前 project object 关联的所有已分类邮件，不只显示最新邮件。
- 附件按照邮件时间倒序排列。
- 点击附件卡片后，后端会用本机默认程序打开该本地文件。

## 新增接口

- `GET /api/projects/<project_id>/attachments`
  - 返回该项目全部附件记录。
- `POST /api/attachments/open`
  - 参数：`projectId`, `attachmentId`
  - 后端根据 SQLite 中的附件记录打开本地文件，不接收前端传入的任意文件路径。

## 注意

附件文件必须真实存在于当前电脑。旧数据库中如果保存的是旧工作目录路径，而文件已经被移动或删除，前端会将该附件显示为不可点击。重新同步 Outlook 后，系统会重新保存附件并刷新路径。

## 本次附件过滤/去重逻辑

后端接口 `GET /api/projects/<project_id>/attachments` 现在在返回前统一处理附件列表：

- 非图片附件：只做去重，不做业务过滤。优先按本地文件内容 hash 去重；如果文件路径不存在，则退回到“规范化文件名 + 文件大小”去重。
- 图片附件：先过滤明显的邮件格式垃圾图，再对保留下来的真实图片做同样去重。
- 垃圾图判断是保守规则，不只看 `image001.png` 这种无意义名称；还会结合文件大小、图片尺寸、logo/signature/icon/banner/pixel 等命名特征。较大的截图或照片会保留。

这个设计把判断放在后端数据出口，而不是前端渲染层，后续如果要接入 AI，只需要在 `backend/services/project_db.py` 的 `is_probably_noise_image_attachment()` 外再增加一个图片分类器即可。

## 测试清理脚本

项目根目录新增 `clean_test.py`：

```bash
python clean_test.py --dry-run
python clean_test.py --all --yes
python clean_test.py --attachments --yes
python clean_test.py --db --yes
```

它会删除本地测试数据中的 `backend/data/attachments` 和/或当前 SQLite 数据库 `backend/data/pm_tracker.db`，并自动处理 `pm_tracker.db-wal`、`pm_tracker.db-shm`。
