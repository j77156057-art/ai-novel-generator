# 大纲级联协议阶段 2-3 设计（kg-cascade-protocol）

> 2026-10-15。目标：补齐接手文档候选 #4——改 plot_bible 的级联链路 + KG 回流大纲。
> 遵循前端既有风格：零新依赖、语义 token、手写组件。**核心约束：旧段不可改。**

## 一、现状盘点

### 已有（不用动）
- `PATCH /projects/{id}` 支持 `plot_bible` 字段（schemas.py:83-94 + routers/projects.py:398-）
- `merged_constraint_items` 把 `plot_bible` 注入 prompt（engine.py:332 + 811 + 958 + 1088）
  → **改 bible 天然影响未来段的角色名/关系/伏笔**（这是「阶段 1：改纲级联」的大部分）
- `outline`（项目大纲）是 `project.outline` JSON 列（models.py:44），**没有** PATCH 端点
- `current_node_prompt` 读 `project.plot_timeline["current_node"]`（engine.py:628-635 路径）——
  节点切换由 `_judge_and_advance` 推进（test_v2_exit_judge.py 46 断言）

### 真缺口（3 件）
1. **改 plot_bible 没有版本/审计**——直接 PATCH 覆盖 dict，事后无从回答「谁在何时把
   '顾辰逸' 改成了'顾辰'」
2. **改 plot_bible 不触发 KG 联动**——之前抽过的 facts 还指向旧设定（例如 bible
   改「顾辰逸是贾正的儿子」→ 已抽的 KgEntity.relation 仍说"romantic"）
3. **大纲冲突没有告警**——bible 改"贾正是董事长"，KG 里有 facts 说"贾正是副总裁"，
   写新段时 LLM 不会知道这是冲突，会自己"和稀泥"瞎写

### 不做（收窄）
- **不改旧段**——已 commit 的 Generation.text 是历史事实，事实不能重写
- **不级联到 node 推进**——`_judge_and_advance` 是基于已写段判定的，
  改 bible 改不动已写段，自然不该倒推节点
- **不引"改纲回退"机制**——版本化是审计不是撤销（可加但本轮不做）
- **不做 Outline 完全覆盖**——plot_bible 是**已选内容的细调**（"父亲是董事长"）
  不是大纲换向；大纲换向是 plot_timeline 的 current_node 改，现状够用

## 二、本轮设计：3 件新增

### 1. plot_bible 版本化（审计最简版）
- 复用 `app.schema_baseline.json` 的设计？——不，那是 DB schema baseline
  不是数据版本
- **新增表** `kg_bible_revisions`（独立表，不挤 Fact）：
  ```sql
  CREATE TABLE kg_bible_revisions (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    revision_no INTEGER NOT NULL,           -- 项目内 1,2,3,4 自增
    bible_json JSON NOT NULL,                -- 该次 patch 后的完整 bible
    changed_fields JSON NOT NULL,             -- diff: 哪些顶层 key 改了
    source VARCHAR(16) NOT NULL DEFAULT 'user',  -- 'user' / 'kg_lift'
    note TEXT,                                -- 备注（手填原因）
    created_at DATETIME NOT NULL,
    UNIQUE(project_id, revision_no)
  );
  ```
- **PATCH plot_bible 时**：先记一行 revision（取项目 max+1），再覆盖。
  不做写入失败回滚——审计先行于正确性。
- **新增端点** `GET /projects/{id}/bible/revisions?limit=20` —— 历史清单
- 集成位置：routers/projects.py 的 PATCH handler

### 2. KG 联动：KG 标记受影响但**不删除**事实
- 改 bible 后**不**自动重抽（LLM 重抽 N 段太贵且不是用户期望）
- 改 bible 后**保留旧 facts**（KgEntity/KgRelation/KgEvent），因为旧段引用过它们；
  **但**新写段时这些 facts **不再进 prompt**（不再"权威"）
- 实现：`merged_constraint_items` 当前读 `reminders + plot_bible`，
  改成读 `reminders + plot_bible + 上一版 bible → diff=removed_keys`：
  - 删了的 fact（key 不在 bible 里）从 constraint_items 排除
  - 新增的 fact 仍按原有路径
  - 不影响已写段（reminders 不重写）
- 集成位置：app/retrieval/queries.py（看现状决定 hook 点）

### 3. 大纲冲突告警（KG 回流大纲的入口）
- 改 bible 后**扫一遍** KG 表：哪些 facts 的 (kind, key) 与新 bible 矛盾？
  例：新 bible 说"父亲不是董事长" vs KG fact "父亲是董事长"
- **不做自动改 KG**——给个**告警清单**：
  - 在 `kg_bible_revisions` 记完的同时，返回一个 `conflicts: [...]` 字段给前端
  - 前端在 PATCH 响应的 toast 里展开：N 条潜在冲突，列出事实原文 + bible 新声明
  - 用户决定：忽略 / 手动去豁免 / 撤回本次 patch
- 冲突判定（极简版）：
  - `kg_foreshadows.name` 改了名 → 视作"作废"（status=dropped）
  - `kg_events.summary` 关键事实矛盾 → 列入冲突但不动作
  - `kg_character_state.new_value` 矛盾 → 同上
  - 匹配规则：bible 顶层 key（如 `characters[name].role`）变化即与对应
    `kg_entities[canonical_name].attrs` 字段比对
- **不做** LLM 冲突判定（成本/准确率/可重复性都不达）——纯字面对比 + 显式字段映射

## 三、明确不做（收窄原因）

- **不做 plot_bible 完全结构化校验**（"bible 必须含 characters/relations/locations"）—
  当前 bible 是 `dict | None`，设计容许缺字段；强行 schema 化会破坏现有项目
- **不做大纲 (project.outline) 的 PATCH**——bible 是细节，outline 是结构，
  改 outline 是另一项（候选 #4 上半），本轮只做 bible 级联
- **不做 kg_events 的作废**——旧事件被旧段文本引用（Generation.text），
  作废 = 改写历史 = 错
- **不做自动重写已写段**——用户改 bible 后看到新未来段即可，**前情**按
  `summary_tree` 滚动有界前情块即可

## 四、工作量

| 项 | 量 |
|---|---|
| kg_bible_revisions 模型 + 迁移 | ~80 行 + 老库补列（无，老库只读 plot_bible JSON） |
| PATCH plot_bible 时记 revision（diff+冲突扫） | ~150 行 |
| 冲突扫描纯字面比对 | ~100 行 |
| merged_constraint_items 排除作废 facts | ~50 行（hook 1 处） |
| 新端点 GET bible/revisions | ~30 行 |
| 前端 PATCH 响应里展开 conflicts toast | ~80 行 |
| 测试（bible revision 写入、冲突检测、删除事实不再入 prompt） | ~250 行 + 30 断言 |
| 端到端：seed 灌 bible → 改 → 看 conflicts 出来 | 0.5d |
| **合计** | **~3d**（与接手文档候选 #4「3-5 天」一致） |

## 五、验证计划

1. 单测：revision 写入幂等（旧 bible 不动不写新行）、diff 算对、
   冲突扫出（bible 改"父亲是董事长"vs 旧 fact"父亲是副总裁"）
2. 单测：merged_constraint_items 排除 fact——bible 把某 character 名删了，
   旧 mention 不再喂给 LLM
3. e2e：浏览器改 bible → 看到 conflicts toast → 决定忽略/撤回
4. 回归：9 套件 431 断言不破
5. 老库兼容：新表 + PATCH 路由向后兼容（旧项目 plot_bible=None 不炸）

## 决策点（问用户）

1. **版本化机制**：推荐**只记"修改后状态"**（新表存完整 bible + diff），
   备选：**也存修改前状态**（额外存 prev_json，多一倍存储可做简单 revert）
2. **冲突告警 UI**：推荐**PATCH 响应里展开**（一次完成，无需新 tab/页面），
   备选：**KG 工作台加第 7 个 tab "冲突"**（持久化但用户看不到就过期）
3. **新表 `kg_bible_revisions` vs 复用 `Fact` 表**：推荐**新表**（kg_* 前缀一致，
   type=kg_bible_revision 会污染 8 类事实枚举），备选：**复用 Fact**
