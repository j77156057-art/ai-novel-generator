# KG v3.0 前端集成设计（kg-v3-frontend）

> 2026-10-15。目标：把 v3.0 的 9 张表 + 读侧 API + Judge 从"后端就绪前端不可见"
> 变成可操作的工作台。遵循前端既有风格：零新依赖、语义 token、手写组件。

## 一、现状盘点

### 前端（Vue3 + TS + Vite，无 UI 库）
- 路由：`/projects/:id`（ProjectWorkspace，7 步向导，已 2669 行）
- API 层：`api/client.ts` 集中封装 fetch；类型集中 `api/types.ts`
- 样式：`style/tokens.css` 语义 token（3 主题）+ `base.css` 复用类
  （card/card-title/row/grid-2/list/list-item/tag/notice/tabs/empty/btn/input）
- 已有相关展示：步骤 7 展示 v2.6 图谱（KgNode 角色/坑）；生成 done 事件里有
  `kg_update`（含 `judge` findings，实时一次性，刷新即丢）

### 后端（已挂载 /api/projects/{id}/kg-v3，7 端点）
slice / slice/prompt / relations/curve / knowledge / rhythm / events / exemptions(GET+POST)

### 缺口（前端做不了的事）
1. **Judge findings 不落库**——engine 算完进 `stats["judge"]` 就丢。哨兵已有成熟
   先例（Fact 落库 + `/alerts` 读回，注释明确"告警落库才谈得上复核"），Judge 应同款
2. **豁免只能加不能撤**——加错一条 (rule, entity) 永久静音该告警，无出口
3. **无角色清单端点**——人物情报页需要"项目里有哪些角色"才能逐个查情报
4. **无关系清单端点**——关系图/列表需要全量边数据，curve 只能给两点

## 二、后端新增（5 项小改动，复用既有模式）

| # | 改动 | 说明 |
|---|---|---|
| B1 | Judge findings 落库 | `judge.py` 加 `save_judge_findings()`：Fact(type="kg_judge", source="judge_v3")，key=`{rule}:{entity}:{segment_no}:{idx}`，props 带 severity/message/evidence。**先清后写同段**（同哨兵 save_findings）。engine 判定后调用 |
| B2 | `GET /kg-v3/judge/findings` | 读回落库告警，按段号倒序 + by_severity + RULES 清单（同 /alerts 形状） |
| B3 | `DELETE /kg-v3/exemptions` | 按 (rule, entity) 删 Fact(type=exemption)。撤销豁免后 Judge 恢复报告 |
| B4 | `GET /kg-v3/characters` | 项目角色清单（id/canonical_name/mention_count/有 secrets 标记），mention_count 降序，上限 200 |
| B5 | `GET /kg-v3/relations` | 关系清单（from/to 名、rel_type、strength），strength 降序，上限 300 |

## 三、前端信息架构

新路由 `/projects/:id/kg`（新 view `KgWorkspace.vue`，**不**塞进 2669 行的
ProjectWorkspace），顶部 tabs（base.css `.tabs`）：

### Tab 1 切片预览
- 输入：段号（默认最新段）+ 窗口/实体数 + 角色名（逗号分隔，可空）
- 展示：`GET /slice` 的 5 块（近期实体/角色状态/未回收伏笔/已知情报/近期事件）
  + `GET /slice/prompt` 渲染文本（mono 块 + 复制按钮）
- 用途：调参时看"生成时 LLM 实际能看到什么"

### Tab 2 人物情报
- 左列：角色清单（B4），带 mention 计数与"有秘密"标记
- 右列：选中角色的已知情报（slice known_facts 同款渲染，`事件：`/`秘密：`前缀标签）
- 底部：`GET /knowledge` 查询表单（角色 + 事实文本 → 知道/不知道）

### Tab 3 事件时间线
- `GET /events` 全量事件按 sequence 升序；event_type 过滤器（下拉）
- 每条：sequence + 类型 tag + summary + in_story_date
- 因果链：links 渲染为条目下方缩进行（`← caused_by seq3`），不画图（线性时间线里链就是引用）

### Tab 4 Judge 面板
- 顶部：规则清单（RULES，含 severity 图例）+ by_severity 计数
- 列表：落库 findings（B2），按段倒序；每条 rule tag（err=红/warn=黄）+ entity
  + message + evidence 折叠
- 每条带「豁免此告警」按钮 → POST exemptions（带 note 输入）
- 已豁免清单（同页下方）：每条带「撤销」按钮 → DELETE（B3）

### Tab 5 关系与节奏
- 左：关系图（B5 数据，**环形静态布局 SVG**：前 N 角色按 mention 环形排布，
  边 strength→线宽/透明度，rel_type→颜色；无物理引擎，确定性布局，零依赖）
- 右：节奏 top 实体（`GET /rhythm`，mention 数 → CSS 横条）
- 下：关系曲线查询（两名字 → `GET /relations/curve`，SVG 折线 + 点标注 note）

### 入口
- ProjectWorkspace 步骤 7 卡片标题行加「KG v3 工作台 →」链接按钮（router-link）
- 不动侧边栏（项目级资源，与助手工作台同逻辑）

## 四、明确不做（收窄）

- **不做** force-directed 力导向图、缩放拖拽（需引库或数百行物理代码，收益低）
- **不做** KG 编辑（KG 是抽取产物，编辑走重新抽取/豁免，不手改）
- **不做** findings 实时轮询（生成中看 done 事件已有；历史看落库清单）
- **不做** v2.6 旧图谱迁移合并（两代并存，旧图谱留在步骤 7）

## 五、工作量

| 项 | 量 |
|---|---|
| B1-B5 后端 + 测试 | ~250 行 + 40 断言，0.5d |
| types.ts + client.ts | ~150 行 |
| KgWorkspace.vue（5 tabs） | ~900 行，1.5d |
| 入口 + 路由 | ~20 行 |
| 构建验证 + 种子数据冒烟截图 | 0.5d |
| **合计** | **~3d** |

## 六、验证计划

1. 后端：`tests/test_kg_v3_frontend_api.py`（新端点 + findings 落库/读回/豁免撤销）
2. 前端：`npm run typecheck && npm run build` 通过
3. 端到端冒烟：种子脚本灌合成 v3 数据（实体/事件/情报/伏笔/mentions/findings）
   → 起后端（非常用端口）+ vite dev → 浏览器逐 tab 截图验证
4. 全量后端套件回归（23 套件保持绿）

## 决策点（问用户）

1. Judge findings 落库（B1）：推荐**做**（哨兵先例+复核价值）；备选：只实时展示不落库
2. 关系图形态：推荐**环形静态 SVG**（零依赖确定性）；备选：仅关系列表+曲线不画图
3. 入口位置：推荐**步骤 7 加链接**；备选：侧边栏加顶层入口
