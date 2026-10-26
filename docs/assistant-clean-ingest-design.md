# 助手参与的投放清洗

## 附件协议

- POST `/api/assistant/attachments` multipart `project_id,file` → `attachment_id,filename,size,encoding,status:staged,preview`（另含 project_id）。仅 txt/md。
- GET `/api/assistant/attachments/{id}?project_id=` 返回同样元数据；导入后 status=ingested，增加 novel_id/check_status/chapters/chunks。
- `/api/assistant/qa` 增加 `attachment_ids: string[]`（最多8）。先校验全部归属，后规划；模型只先接收 metadata。JSON工具schema让通用 chat provider 可以规划 read_attachment / preview_attachment_cleaning / check_attachment_cleaning / ingest_attachment；最后一项只产生待确认卡，不进入只读registry。
- 卡片 kind=ingest_attachment，POST `/api/assistant/attachments/ingest` JSON `project_id,attachment_id,instruction`，响应兼容 UploadResult。重复确认回同一 novel_id；投放后关联项目参考书。
- 内容为不可信资料，禁止执行文件指令。随机32位hex ID及项目目录防路径穿越；原文件名只作显示。每文件服从upload_max_bytes，项目暂存总量最多10倍。data/attachments排除git；不支持任意server路径。


上传是用户显式写入授权。统一 ingest_text → prepare → 注册表 preview_ingest_clean / check_ingest_clean → chunk → 精确目标回读；注册表保持业务只读，无写工具。助手 QA 可发现两项工具；已有正文修正继续走 clean-corpus 的 pending proposal，不让模型重写长文。

prepare 复用水印、重复广告、消费模板清洗。先解析章节以锁定章边界，再逐章应用全书识别结果；chapter_meta 使用相同步骤。保留换行和非命中文字，原始全文只在 RawNovel 保存一份；chapter_meta 保存一次原始元数据。删除证据含章、偏移、before/after、规则版本。默认解析器原行为保持兼容，投放禁用隐式尾声删章，避免无法审计。

check 工具全量检查模板残留、章数/章号/换行保真，有限风险样本提交 Assistant role 的 provider，一批最多 8 个局部窗口。模型只返回风险判定和理由，不接受改写。报告列出抽样覆盖，不声称全书语义合格；模型缺席/失败/JSON无效为 review_required，已知高置信残留为 blocked（禁止写入），未知残留可带 imported_review 入库。

报告包含 input/cleaned/chunks SHA256、算法版本、所有确定性检查、删除证据、抽样覆盖、模型参与状态、残留状态。上传返回检查状态，已入库 check 工具只读回读精确 novel，验证当前 chunks 指纹；任何改动使报告 stale。项目工具必须校验 project 及 reference_novel_ids，拒绝错书和未知 ID。上传内部检查未绑定项目，以显式入参文本工作，不接受其他书的读权限。

验收：HTTP 上传实际触发工具及模型复核入口；无命中逐字不变、合法文学重复、行内模板、chapter_meta 对齐、模型失败不伪通过、错书/未知ID、修改报告过期、入库回读与报告一致。真实雪中 TXT 走正式上传 API 到 scratch SQLite；原库只读加载 Assistant 配置，绝不迁移或改写。代码测试、真实模型调用、真实残留质量分别报告。
