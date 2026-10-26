# 文风蒸馏 v2.1 增量设计（落库 + 前端 + 主链审计）

> 2026-10-16。**性质：v2-survey.md 调研后的**具体增量实施**。本轮只补 3 件：
> style_warning 落库 / 前端面板 / audit_kg 主链挂载。其他 v2 设想（文笔 DNA / 作品蓝本
> / 像不像审计维度）master 已落地（见 v2-survey.md 二节），**不做**。

## 一、3 个真缺盘点

master audit 后的真缺（其他已做）：

| 项 | 当前 | 缺什么 |
|---|---|---|
| **Q1 落库** | `style_warning` 在 return dict 里**生成完即丢** | 落 `projects.style_narrative.style_warnings` 数组，**段级历史** |
| **Q2 阈值** | `STYLE_WARN_PERCENTILE = 97.5`（留出集分位） | ✅ 已有，**不动** |
| **Q3 前端面板** | 前端 0 处引用 `style_warning` | 渲染 4 维 / 20 维 + percentile 曲线 |
| **Q4 主链 audit_kg** | 助手页手动跑 | 每段 generate 后**自动调**（被动审计） |

## 二、Q1 落库：style_warning 落 `style_narrative.style_warnings[]`

### 2.1 schema（在现有 `style_narrative` JSON 字段加 `style_warnings` 数组）

```json
{
  "version": ...,
  "rules": {...},             // 现有 v7 14 维度规则
  "forbidden": {...},         // 现有
  "rejected": {...},          // 现有
  "fingerprint": ...,         // 现有
  // v2.1 新增：
  "style_warnings": [
    {
      "segment_no": 5,
      "generated_at": "2026-10-16T12:34:56Z",
      "percentile": 99.2,
      "mean_abs_z": 1.85,
      "worst_features": ["比喻密度", "短句占比", "段长CV"],
      "warn_message": "本段文风偏离档案（percentile 99.2...）"
    }
  ]
}
```

### 2.2 改 2 处

1. `app/generation/engine.py` line 880-920 区段，`_style_warning` 计算后**追加**到返回 + **写库**：
   ```python
   style_warning = _style_warning(text, _style_profile(db, project))
   if style_warning:
       _append_style_warning(db, project, segment_no, style_warning)
   ```
2. 新建 `app/style/warning_store.py` 单一函数 `_append_style_warning(db, project, seg_no, warn_dict)`：
   - 读 `project.style_narrative`（已有 fingerprint / rules 字段）
   - append 一个新条目到 `style_warnings` 列表
   - commit
   - **append 而非 replace**——段级历史

### 2.3 不做的事

- ❌ 不新建 `ProjectStyleWarning` 表——`projects.style_narrative.style_warnings[]` JSON 即可
- ❌ 不限制数组长度——长期项目可能 1000+ warn，存得住（SQLite 大字段）
- ❌ 不异步写——engine 同步落库可观测（不阻塞生成的也仅 1 次小 UPDATE）

## 三、Q3 前端面板

### 3.1 入口

`frontend/src/views/ProjectWorkspace.vue`（已在用，KG 工作台 tab）。
加一个新 subtab **「Style 审计」**（和现有「切片预览/人物情报/.../Worker 状态」平级）：
- 调 `GET /api/projects/{id}/kg-v3/style-warnings`（**新端点**）
- 展示：
  - 顶部：阈值说明「percentile ≥ 97.5 触发告警」（用户可调）
  - 折线图：每段 percentile（绿/黄/红颜色编码）
  - 列表：warn 详情，含 worst_features 链接到「人物情报」/「事件时间线」
- 「重新审计」按钮：调 `POST /api/assistant/audit-kg`（已存在路由）

### 3.2 改 2 处

1. `frontend/src/api/client.ts`：加 `getStyleWarnings(projectId)` + 类型
2. `frontend/src/views/ProjectWorkspace.vue`：加 subtab + 折线图（用 ECharts 或纯 SVG——项目里 ECharts 已用）

### 3.3 不做的事

- ❌ 不做实时更新（用户进 Style 审计 tab 时拉一次，OK）
- ❌ 不做 20 维雷达图（首版只 percentile 折线 + 列表——保持「精巧」）

## 四、Q4 主链挂 audit_kg

### 4.1 思路

**audit_kg 是图谱级审计**（孤儿节点 / 角色名冲突 / foreshadow 缺等），**与 style_warning 是两件事**。
本轮**只挂**一种：每 N 段（默认 5）自动跑一次 `audit_kg` 异步。

但 audit_kg **是 LLM 密集**（agent 调用）——挂主链**会拖慢 generate**。
**轻量方案**：每 5 段 generate 后**异步触发** audit_kg（fire-and-forget），结果**落 AgentRun 表**（已存在）+ 助手页可见。

### 4.2 改 1 处

- `app/generation/engine.py` 末尾（或 `_post_commit_written` 之后）：
  ```python
  if segment_no % AUTO_AUDIT_INTERVAL == 0:
      _schedule_async_audit_kg(db, project)
  ```
- `_schedule_async_audit_kg` 用 `threading.Thread(daemon=True)` 启动 agent 调用，**不阻塞** generate

### 4.3 不做的事

- ❌ 不在主链同步等 audit_kg（拖慢生成）
- ❌ 不每段都跑 audit_kg（5 段一跑足够）
- ❌ 不做 audit_kg 失败重试（agent 有自身重试）

## 五、改动汇总

| 文件 | 改 | 行 |
|---|---|---|
| `app/style/warning_store.py` | 新增 | ~30 |
| `app/generation/engine.py` | 2 处（落库 + 异步审计调度） | ~30 |
| `app/routers/kg_v3.py` | 新增 `GET /api/projects/{id}/kg-v3/style-warnings` | ~20 |
| `frontend/src/api/client.ts` | 新增 `getStyleWarnings()` | ~10 |
| `frontend/src/views/ProjectWorkspace.vue` | 新增 Style 审计 subtab | ~80 |

**总 ~170 行**（后端 80 + 前端 90）。

## 六、测试

新增 `tests/test_style_warning_persistence.py`：
- 生成一段触发 97.5 percentile 边界 mock
- 验 `projects.style_narrative.style_warnings` append 成功
- 验多段不覆盖
- 验 GET 端点返回

## 七、风险

- **style_narrative JSON 字段膨胀**：长期项目 1000+ warn 条目 1-2MB——SQLite 撑得住，
  读取会慢。降级：超过 500 条只保留最近 500 + 提示「历史已截断」
- **前端 subtab 让 ProjectWorkspace 更长**：当前 6 tab → 7 tab，UI 拥挤。降级：把 Style
  审计 + Judge findings 合并到一个「质量」大类
- **AUTO_AUDIT_INTERVAL=5 对小项目偏频繁**：降到 10（或可配）
