# 前端交接单（给外部协作者 / 另一个 AI）

> 2026-11-02。这份文档的目标：**别人接手前端也能改对，并且不会撞坏已验收的东西**。
> 先把「什么不能碰」讲清，再讲「该做什么」，最后给可验证的自检命令。

---

## 0. 一分钟上手

```powershell
cd <REPO_ROOT>/frontend
npm ci            # 首次
npm run dev       # dev server → http://localhost:5173
npm run typecheck # vue-tsc --noEmit（必须过）
npm test          # 66 个纯逻辑用例（node --test）+ 33 个组件用例（vitest）
npm run build     # 上面两条都过，才会产出 dist/
```

后端（另一个终端，可选）：

```powershell
cd backend
$env:PYTHONIOENCODING='utf-8'
python -m uvicorn app.main:app --port 8000 --host 127.0.0.1
```

没有后端也能跑 `typecheck` / `test` / `build`；
但要**看到真界面**，需要后端在 8000，前端会打 `/api/*`。

---


> ⚠️ **本仓库不写本机绝对路径**：上面的 `<REPO_ROOT>` / `<WORKSPACE>` / `<TMP>`
> 都是**占位符**（本机上的真实路径每次可能不同，由助手在当次对话里给出）。

## 0'. 本轮怎么干（**先读这一节**）

| 项 | 值 |
|---|---|
| **仓库路径（就用这棵）** | `<REPO_ROOT>` |
| **分支** | `feat/ui-trae`（已从 `main` 建好，**别在 `main` 上直接改**） |
| 上一轮那版 UI 的**参考补丁** | `<TMP>/trae_ui_reference.patch`（196KB，`git apply` 可看全貌；**不要直接 apply 到发布版**） |
| 上一轮那版 UI 的**原始提交** | dev 树 `ai-novel-generator` 的 `91c4970`（10 个文件，+1267 / −515） |

### ⚠️ 为什么不能直接拷文件 / 直接 apply 补丁

上一轮那版 UI 是在**另一棵更旧的树**上做的（dev 树：`<WORKSPACE>/ai-novel-generator`）。
两棵树已经**双向分叉**：

| | 仅发布版有 | 仅 dev 树有 | 两边都有但内容不同 |
|---|---|---|---|
| `backend/app` | **83 个文件** | 2 个 | **59 / 75** |
| `frontend/src` | **8 个文件** | 0 个 | **22 / 23** |

⇒ 直接覆盖会**删掉发布版的新功能**（见下），必须**在发布版这棵树上重做**。

### 这棵树上比那棵树多出来的东西（**别删、别绕过**）

| 新增 | 位置 | 作用 |
|---|---|---|
| 助理附件链路 | `utils/assistantAttachments.ts`、`components/AssistantIssueList.vue` | 上传 → 暂存 → 投喂；`tests/assistantAttachments.test.ts` 在守 |
| 参考书边界 | `utils/referenceBoundary.ts` | 不许把原文整段带进提示词；`tests/referenceBoundary.test.ts` 在守 |
| 外文块提示 | `components/ForeignBlocksNotice.vue` | 「混进了别的书」的体检提示；`tests/foreignBlocksUi.test.ts` 在守 |
| 工作流条 | `components/WorkflowStrip.vue`、`utils/workflowDisplay.ts` | 创作流程可视化；`tests/workflowDisplay.test.ts` 在守 |
| 角色扮演面板 | `components/RoleplayPanel.vue` | 角色对话 |
| 语料体检报告 | `utils/corpusReport.ts`、`utils/measure.ts` | 语料度量与报告 |
| **全书摘要面板** | `views/CorpusView.vue` | 摘要提交/进度/三块内容/四项验收/丢弃原因 |
| 前端测试体系 | `frontend/tests/`（11 个逻辑用例文件 + 4 个组件 spec） | `npm test` = 58 + 33，**必须全绿** |

### 本轮该做的三件事

1. **UI 改版在发布版这棵树上重做**（上一轮那版可作视觉参考，补丁在 `<TMP>/trae_ui_reference.patch`）；
2. **G1 导出三件套** —— 正文之外还有大纲 / 人物表 / 坑清单（数据都在，缺拼装与入口）；
3. **G2 登录占位** —— 第一屏是登录（单用户占位，**不做真鉴权**，也**不要**用登录态去拦 `/api/*`）。

### 本轮不能弄红的东西（与上一版交接单相同，再强调一次）

```powershell
cd <REPO_ROOT>/frontend
npm run typecheck   # vue-tsc --noEmit，必须 0 错误
npm test            # 58 + 33，必须全绿
npm run build       # 上面两条都过才产出 dist/
```

---

## 1. 现状（不要凭直觉假设）

| 项 | 实际 |
|---|---|
| 框架 | **Vue 3.5 + Vite 6 + TypeScript**（任务书原写 React，见 §4 G4） |
| 路由 | `vue-router 4`，8 个页面（见下表） |
| 状态管理 | **没有 Pinia / Vuex** —— 状态都在各 view 的 `ref`/`reactive` 里 |
| UI 库 | **没有**（无 Element/Ant/Nuxt UI）—— 样式在 `src/style/tokens.css` + `base.css` + 组件内 `<style scoped>` |
| HTTP | `src/api/client.ts`（**47KB，手写 fetch 封装，120 个方法**，无 axios） |
| 类型 | `src/api/types.ts`（**66KB**，与后端 schema 一一对应） |
| 测试 | `frontend/tests/*.test.ts`（58，`node --test`）+ `tests/components/*.spec.ts`（33，vitest） |
| CI | `.github/workflows/ci.yml` 的 frontend job：`npm ci` → `npm test` → `npm run build` |

### 页面路由表

| 路径 | 组件 | 作用 |
|---|---|---|
| `/` | — | redirect → `/assistant` |
| `/assistant` | `AssistantResolver.vue` | 助理入口（选项目 / 选语料） |
| `/corpus` | `CorpusView.vue` | 语料库（投放、体检、**全书摘要面板**） |
| `/projects` | `ProjectsView.vue` | 项目列表 |
| `/projects/:id` | `ProjectWorkspace.vue` | **主工作台（196KB，巨型单文件）** |
| `/projects/:id/assistant` | `AssistantWorkspace.vue` | 助理工作区（54KB） |
| `/projects/:id/kg` | `KgWorkspace.vue` | 知识图谱（46KB） |
| `/mine` | `MyNovels.vue` | 我的作品 |
| `/settings` | `LlmSettings.vue` | 渠道与模型设置 |

### 组件

`AssistantIssueList`（8.7KB）/ `DistillProgress`（10.8KB）/ `ForeignBlocksNotice`（4.2KB）/
`RoleModelPanel`（21KB）/ `RoleplayPanel`（15KB）/ `WorkflowStrip`（19KB）。

### 接口规模（交接方最容易低估的一点）

后端 **126 个端点**，`client.ts` 里封了 **120 个方法**。
这也是 `ProjectWorkspace.vue` 长到 196KB 的一半原因 —— **不是 UI 复杂，是功能面太宽**。

权威契约：**起后端后直接读 `/openapi.json`**（约 244KB，FastAPI 自动生成，含全部 schema）：

```powershell
# 后端已在 8000 时
curl http://127.0.0.1:8000/openapi.json -o <TMP>/openapi.json
# 或者直接在浏览器打开 http://127.0.0.1:8000/docs （Swagger UI，可交互）
```

按域分组（只列主要的，完整见 `/openapi.json`）：

| 域 | 端点前缀 | 大致用途 |
|---|---|---|
| 项目与档案 | `/api/projects/*` | 项目 CRUD、文风蒸馏、叙事档案、角色模板、设定（背景/力量体系/规模）、伏笔提醒、角色卡、**导出** |
| 生成 | `/api/generate*`、`/api/regenerations`、`/api/alerts` | 分段生成、流式、循环、计划、告警 |
| 知识图谱 | `/api/projects/{id}/kg*`、`/kg-v3/*` | 切片、关系曲线、事件、豁免、判官发现、角色/关系、worker 状态 |
| 语料 | `/api/novels/*` | 列表、分片、检索（直传上传已主动关闭 → 409，走助理附件） |
| 全书摘要 | `/api/projects/{id}/digests`、`/api/books/{id}/digest` | 提交摘要任务、取产物、取验收 |
| 助理 agent | `/api/assistant/*` | 问答、诊断、附件、审计、清洗、提案（编辑/行动）、版本、提示词 |
| 设置 | `/api/settings/*` | 渠道与模型、生成档位、连通性测试 |
| 向导 | `/api/projects/{id}/wizard*`、`propose-premise`、`expert-questions`、`generate-outline` | 专家模式（wayfinder 追问构思） |
| 角色扮演 | `/api/projects/{id}/roleplay/*` | 角色对话 |

⚠️ **`GET /api/novels/upload` 这类直传是 409（有意关闭）** —— 别把它当 bug 修。
它返回的 `detail` 里写了原因与替代入口（`/api/assistant/attachments`）。

---

## 2. 硬约束（弄红了就是回归，必须自己先跑绿）

1. **`npm run typecheck` 必须过** —— `vue-tsc --noEmit`，CI 的 `build` 里就含它。
2. **`npm test` 必须全绿**（58 + 33）。这 91 条里有几条是**契约测试**，不是随手写的：
   - `jsonBodyContentType.test.ts` —— 请求体必须是 `application/json`（不然后端 422/415）；
   - `referenceBoundary.test.ts` —— 参考书边界（不许把原文整段带进提示词）；
   - `styleBlockBraces.test.ts` —— 提示词模板的花括号转义（曾因 `{}` 打架炸过）；
   - `assistantAttachmentApi.test.ts` / `assistantAttachments.test.ts` —— 附件暂存链路；
   - `assistantReactNotice.test.ts` —— **进对话前必须先交付附件**的提示逻辑。
   ⇒ 改这些行为之前先看测试在守什么，别直接改测试迁就实现。
3. **不要引入重型依赖**（UI 库 / 状态库 / axios）而不说明理由 —— 现在这套是「零运行时依赖除 vue + vue-router」，
   一旦引入，`npm ci` 体积、CI 时间、离线可用性都会变。要引就在 PR 描述里写清收益。
4. **不要动 `src/api/types.ts` 的既有字段语义** —— 它和后端 `backend/app/schemas.py` 一一对应；
   要加字段就加，不要改名字/改含义（后端会 422）。
5. **界面文案口径**：书面、克制、专业。不要用「就能 / 就行 / 它会」这类口语；
   也不要堆术语。正确样例：现状「当前项目尚无任务。」/ 动作「可在顶部「开工配置」中描述目标。」/ 按钮「配置目标」。
6. **长清单默认收起 + 按属主分组 + 每条带状态**（这是主人明确纠正过两次的偏好）：
   别把二十几行平铺在正文里；每组标明「有几个」。

---

## 3. 该做什么（按任务书缺口排）

任务书原文见 §4；逐条对照在 `docs/mvp-spec-gap-analysis.md`。前端相关的两个缺口：

### G1（优先）导出三件套

任务书 §2.6 要求生成环最后一步产出**`.txt` + 大纲 + 人物表 + 坑清单**。
现在只做了正文：

- 已有：`GET /api/projects/{id}/export-txt` → `client.ts::api.exportTxt` → `ProjectWorkspace.vue::runExport`（仅正文 + 一行元信息）
- 数据都在，**缺拼装与入口**：
  - 大纲：`projects.outline`（`ProjectOut` 里有 `outline` 字段）
  - 人物表：`GET /api/projects/{id}/character-cards`
  - 坑清单：KG 伏笔 + `GET /api/projects/{id}/reminders`
- 建议做法：**不改后端也能先做**——前端把三份数据取回来，在浏览器侧拼成一个 zip 或多文件下载；
  或后端加一个 `export-bundle`（更好，但要动后端 ⇒ 需要后端同学配合）。

### G2（优先）登录占位

任务书 §2.7 要求第一屏是登录（单用户占位，**不做真多用户隔离**）。

- 现状：`/` 直接 redirect 到 `/assistant`，无 `/login`
- 建议：加 `/login` 路由 + 一个占位页（一个用户名输入 + 进入按钮，前端存 localStorage，不校验密码）
- ⚠️ **不要**做成真鉴权（任务书明写「不做真多用户隔离」）；也**不要**让未登录状态挡住 `/api/*` 调用

### 其他值得做（任务书没写，但这是简历项目）

- **`ProjectWorkspace.vue` 196KB 拆分**（最大隐患）：按「创作区 / 验收区 / 导出区 / 角色卡区」拆成子组件。
  这是纯重构，**验收标准就是 91 条测试 + typecheck 全绿 + 手工点一遍主链路**。
- 空状态与错误态：现在多处 `alert()`（`ProjectWorkspace.vue:2041` 等），换成内联提示更专业。
- 无障碍与键盘可达（现在基本没有）。

---

## 4. 任务书原文（摘要，发布版不含 PRD）

来源：工作区的 `ai-novel-generator-plan.md`（2026-10-12 wayfinder 关图 → to-spec → MVP Spec v2）。

| 任务书要求 | 现状 | 处置 |
|---|---|---|
| 完整 Web（登录占位 / 项目列表 / 新建 / 进度 / 预览 / 下载 / 历史） | 8 页齐全，**缺登录占位** | G2 |
| 导出 `.txt` + 大纲 + 人物表 + 坑清单 | **只有正文** | G1 |
| React + Vite | **Vue 3 + Vite** | 不可改（代码已沉淀），已在 README 说明 |
| 分段 3k–5k 字/轮 | 现默认 1200 字/段（`chunk_size`），可配 | 如需改，先与后端确认 |
| 单用户占位、不做真多用户隔离 | 符合 | — |

---

## 5. 提交前自检清单

```
cd <REPO_ROOT>/frontend
npm run typecheck   # 必须 0 错误
npm test            # 必须 58 + 33 全绿
npm run build       # 必须产出 dist/
```

手工点一遍（有后端时）：

1. `/projects` 能看到项目列表；
2. 进一个项目，生成 1 段（或看已有产物）；
3. 导出 txt 能下载、文件名正确（中文名走 RFC 5987）；
4. `/corpus` 选中一本书 → 全书摘要面板能显示（若已有产物）；
5. 改坏的路径：断网/后端停掉时，页面要有**可读的错误提示**，不是白屏。

---

## 6. 别做的事

- ❌ 不要改 `backend/` 的判定口径 / 阈值 / 提示词（那是另一条线，改了会让验收数字失效）；
- ❌ 不要把真实作品书名 / 人物名 / 原文引句写进仓库（发布版有卫生要求，`skills/blind-packet-deanonymization` 有扫描脚本）；
- ❌ 不要引入 `console.log` 留痕；
- ❌ 不要把密钥写进前端（模型 key 只经后端环境变量）。
