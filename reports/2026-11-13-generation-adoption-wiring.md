# 生成侧接入检索新工具的六处改造与实跑（2026-11-13）

> 范围：只改 `generation/` 与 `agents/` 的提示词侧，`retrieval/` 下的实现一行未动。
> 开关：`author_research_enabled` 默认 `False`；本次实跑用进程级环境变量临时开启，进程结束即恢复，`.env` 未改。
> 环境：project 6《本群禁止飞升》，original 档，`distill_id=4`（27,015 张卡），MiniMax-M3.1-Flash。

---

## 一、六处改前 / 改后对照

| # | 位置 | 改前 | 改后 |
|---|---|---|---|
| 1 | `author_research._judge_call` | 自带一套字面判定：`_UNIT_RE` / `_QUOTED_RE` 抽数字与引号短语，只认字面出现；用模块内硬编码的 `_ENTITY_WORDS`（本项目人物/物件名）做实体命中 | 委托 `retrieval.adoption.judge_call`，`TermWeights` 由 `TermWeights.from_cards(load_catalog(distill_id).cards)` 构建并按 `distill_id` 缓存（`_WEIGHTS_CACHE`）；硬编码实体词、中文停用词表、`re` 与 `RetrievalLog` 导入一并删除。换书重建卡表即自动换权重 |
| 2 | `author_research.check_adoption` | 自己统计两档 `adopted / not_detected / unjudgeable / nav`，返回 `counts/shares/per_call` | 委托 `retrieval.adoption.judge_calls`，返回 `counts/shares/per_call` 原样透传（新实现已兼容这三个字段名）；仅把原调用上的 `args / gap_metric` 按 `seq` 补回逐条明细，保 `quality_json` 旧读法不炸 |
| 3 | `author_research.backfill_adoption` | 自己遍历调用，只对「判定为采纳」的调用写 `retrieval_logs.adopted_rule_ids`，未采纳行留空 | 委托 `retrieval.adoption.backfill_adoption` → `trace.fill_segment_adoption`，按 `session_id + segment_no` 更新**该段所有行（含被拒、empty、error）**；`session_id/segment_no` 缺失或写库异常时回滚并退化为纯判定，不弄挂生成。`run_author_research` 的 `finally` 增加 `out["segment_no"]`、`out["distill_id"]`，供回填取用 |
| 4 | 助理 agent 工具清单 | 注册表 `ALLOWED_READ_ONLY_TOOLS` 与 `_DEFAULT_SPECS` 已含 `inspect_retrieval_trace`，但模块顶部手维护注释只写到五级目录工具 | 注释补上「作者轨迹监察（inspect_retrieval_trace）」，并明确「模型可见说明的唯一来源是 `_DEFAULT_SPECS.description`，新增只读工具两处一起加」。核查结论见 §2.4 |
| 5 | `prompts._catalog_skeleton_block` / `_catalog_path_keys` 的模型可见标题 | `【路径键（工具只认英文键，别用中文标签拼 path）】`——与工具现状矛盾（工具已归一），会把模型往「先学键名」上引 | `【路径键（优先用英文键；中文标签也会被工具归一，可直接使用）】`；两处 docstring 同步改为「工具侧已归一，照标签拼路径不会再空转」 |
| 6 | `author_research._system_prompt` 末行 | 「预算快用完会强制停」式表述，读者会理解成「到轮次就中断」 | `预算接近上限时工具会自动收窄返回（条数变少、窗口变窄），按需要继续读即可；不要重复读同一条` —— 与工具内预算池口径一致 |

附带的必要改动（为第 1–3 条服务，不是额外需求）：

- `_record` 的 call dict 增补 `path` 与 `rule_id`：`judge_calls` 只认这两个字段取规则原文，缺了就全落 `unjudgeable`。
- `run_author_research` docstring 的「3–8 轮」改为「3–8 次」，避免与第 6 条口径冲突。

**未改**：`_MAX_SEARCHES` / `_MAX_MODEL_ROUNDS` / `_MAX_MODEL_RULES` / `_MAX_ROUNDS_SAFETY` 四个常量保持原值，作为内部安全阀；第 6 条只改提示词说给模型的那句话，不改代码行为。

---

## 二、静态核对

### 2.1 编译

三个改动文件 `py_compile` 全部通过：`generation/author_research.py`、`generation/prompts.py`、`agents/registry.py`。

### 2.2 遗留引用

全仓 Grep `_ENTITY_WORDS`、`_CN_STOP`、`_UNIT_RE`、`_QUOTED_RE`：无残留。

### 2.3 调用方签名

`engine.py` 两处 `backfill_adoption(db, research, text)` 与一处 `check_adoption(...)` 签名未变，无需改动。

### 2.4 第 4 条的落点核查

仓库里不存在手写的「助理 system prompt 工具清单」：模型可见的工具说明唯一来源是 `agents/registry.py::_DEFAULT_SPECS[].description`，该处已有 `inspect_retrieval_trace`（`registry.py#L1007-L1018`），描述里写明返回 `healthy/watch/drifting` 与「用户问『这段检索是不是跑偏了』时调用」。因此本条实际只补了手维护白名单注释（`registry.py#L61-L64`）。如果日后另加提示词清单，需要以 `_DEFAULT_SPECS` 为准，不要再抄一份。

---

## 三、项目 6 实跑（临时开启）

三个场景，同一进程内校验 `enabled_at_start / enabled_at_end` 均为 `True`；跑完另起干净进程验证 `settings.author_research_enabled is False`。

### 3.1 场景 A：段 19（5,404 字，正常自查）

| 项 | 值 |
|---|---|
| session_id | `author-p6-s19-563591` |
| 调用数 / 轨迹行数 | **5 / 5**，`trace_match=true` |
| `seq` | 1,2,3,4,5（无缺口、无重复） |
| 状态 | 全 `ok`（空转率 0%） |
| 工具分布 | search 2 / rule 3 |
| 终止原因 | `duplicate_call`（第 6 次想重复已读规则，被去重拦下） |
| 自查成本 | 3 次 LLM 调用，1,812 prompt + 88 completion tokens，9.38s |
| 采纳分布 | `not_detected 2 / unjudgeable 1 / nav 2`，`adopted_rule_ids=[]` |
| 监察 verdict | **watch**（唯一 finding：`adoption_missing`，读了 3 次规则但无采纳回填） |
| 中文路径探针 | `style` 与 `文风规则`：均 `ok`、3 hits、`resolved=style`、`unknown=null` |

### 3.2 场景 B：段 9（3,106 字，正常自查）

| 项 | 值 |
|---|---|
| session_id | `author-p6-s9-563624` |
| 调用数 / 轨迹行数 | **3 / 3**，`trace_match=true` |
| `seq` | 1,2,3 |
| 状态 | 全 `ok` |
| 工具分布 | search 1 / rule 2 |
| 自查成本 | 2 次 LLM 调用，886 prompt + 50 completion，6.18s |
| 采纳分布 | `not_detected 1 / unjudgeable 1 / nav 1` |
| 监察 verdict | **healthy**，无 findings |
| 中文路径探针 | 同 A：两侧都 `ok`、3 hits |

### 3.3 场景 C：段 9 强制触发「短句占比」缺口（证明非零采纳可达）

正常自查时模型挑的规则恰好没被正文用上，采纳为 0——这既可能是判定失效，也可能是事实。为区分，用同一段正文把缺口指定为「短句占比」（该缺口对应的规则确实被正文改写使用），其余不变。

| 项 | 值 |
|---|---|
| session_id | `author-p6-s9-563775` |
| 调用数 / 轨迹行数 | **4 / 4**，`trace_match=true` |
| `seq` / 状态 | 1,2,3,4，全 `ok` |
| 工具分布 | search 1 / rule 3 |
| 自查成本 | 3 次 LLM 调用，1,420 prompt + 78 completion，6.71s |
| 采纳分布 | **`adopted_rewrite=1`（1/3，0.333）**、`unjudgeable=2`、`nav=1`；`adopted_rule_ids=["19695e3436dbc856b316"]` |
| 证据 | 该规则「用问号、打断和短句制造吐槽节奏」的高 IDF 词在正文里成簇出现 |
| 监察 verdict | **healthy**，`metrics.adopted_rows=4` |
| 中文路径探针 | 同 A/B |

### 3.4 历史真实作者会话重判 + 数据库核对

用新链路（`judge_calls` + `backfill_adoption`）重判项目 6 库里已有的四个真实作者会话，直接读回数据库确认落地：

| session | 轨迹行数 | 采纳条数 | 写入 `adopted_rule_ids` 的行数 | 涉及规则 id 数 |
|---|---|---|---|---|
| `author-p6-s9-505215` | 32 | 4 `adopted_rewrite`（44.4%） | **32 / 32** | 4 |
| `author-p6-s9-505427` | 31 | 5 `adopted_rewrite`（45.5%） | **31 / 31** | 5 |
| `author-p6-s10-506096` | 7 | 1 `adopted_rewrite`（33.3%） | **7 / 7** | 1 |
| `author-p6-s11-506387` | 7 | 1 `adopted_literal` + 1 `adopted_rewrite` | **7 / 7** | 2 |
| `author-p6-s9-563624`（场景 B） | 3 | 0 | 0 / 3 | 0 |
| `author-p6-s9-563775`（场景 C） | 4 | 1 | **4 / 4** | 1 |
| `author-p6-s19-563591`（场景 A） | 5 | 0 | 0 / 5 | 0 |

结论：**新回填确实写该段所有行**（含 `empty`/`error`/未采纳行），与旧实现「只写采纳行」的行为差异在数据上可见；重判的历史会话采纳率 33%–45%，与本报告 §1 第 1 条「旧判定只认字面、把改写式采用误判为未检出」的判断一致。

### 3.5 与改前的对照

| 指标 | 改前（旧实现，见 `2026-11-13-p6-author-research-wiring.md` §2.4–2.5） | 改后 |
|---|---|---|
| 判定档位 | `adopted / not_detected / unjudgeable / nav` 四档，只认字面 | `adopted_literal / adopted_rewrite / not_detected / unjudgeable / nav` 五档，含 IDF 词面簇、分句重合、章末/开篇位置线索 |
| 实体/停用词来源 | 模块内硬编码本项目人物与物件名 | 卡表二元组 IDF，按 `distill_id` 缓存（本项目 `len(idf)=156,278`，首次构建约 1.65s） |
| 回填范围 | 只写采纳行 | 该段所有行 |
| 中文路径 | 32 次调用里 6 次（19%）空转 | 探针 `style` / `文风规则` 均为 `ok`、3 hits、`resolved=style` |
| 采纳结果 | 真实作者会话 32 次调用 `adopted=0` | 同一会话重判 4 条 `adopted_rewrite`；强制场景 1 条 `adopted_rewrite` |
| 提示词路径键措辞 | 「工具只认英文键，别用中文标签拼 path」 | 「优先用英文键；中文标签也会被工具归一」 |
| 提示词预算口径 | 会读成「到轮次强制中断」 | 「接近预算时工具自动收窄返回」 |

---

## 四、开关状态

- `app/config.py::author_research_enabled` 默认值 `False`，本次**未改**配置与 `.env`。
- 实跑期间靠 `$env:AUTHOR_RESEARCH_ENABLED="true"` 在单个 PowerShell 进程内临时开启；三个场景的 JSON 里 `enabled_at_start / enabled_at_end` 均为 `true`，说明开启只在该进程内生效。
- 跑完另起干净进程验证：

```
author_research_enabled = False
type = bool
```

即已恢复默认关闭。这是**临时行为，不是把功能改成默认开**。

---

## 五、遗留与风险

1. **正常自查的采纳仍可能是 0，且这是真实结果，不是接线失败。** 段 19、段 9 的 0 采纳都有完整的 `judge_calls` 明细（`not_detected` / `unjudgeable` / `nav` 各几条、原因逐条可读），且同段强制触发后能判出 `adopted_rewrite`。判定链路、权重构建、回填写库三段都已分别被验证。
2. **`unjudgeable` 占比偏高（场景 A/C 均 50% 以上）**：纯叙述型规则没有可核对标记，也没有高 IDF 词，按口径就该判不了，没有硬凑成 `not_detected`。这是设计选择，已在 `retrieval/adoption.py` 的模块 docstring 里写明。
3. **`search` 调用一律算 `nav`**，不参与采纳统计；因此「search 摘要里看到的规则是否被用上」目前不可判。若后续要判，需要让 search 结果带 `rule_id` 逐条判定，属于检索侧改动，本次未做（越界）。
4. **`TermWeights` 首建约 1.65s**，已按 `distill_id` 缓存；同进程跨段复用，不重复构建。跨进程（每次生成一个新 worker）仍会重新构建，暂无跨进程缓存。
5. **提示词轮次口径只改了文本**：`_MAX_MODEL_ROUNDS=3` 等常量仍在，模型看到的是「工具会收窄」，代码层仍是硬上限。若希望两者完全一致，需要单独评估是否放开安全阀，本次按要求未动。
