# 任务交接：时间线规划器（feat/timeline-pacing）

> 交接时间：2026-10-15。新窗口接手请先读本文，再读 docs/timeline-pacing-design.md。
> 目标：把「一次投喂整条故事线 → 第一章就完结」改成「按剧情点分段投喂，进度条驱动」。

## 一、环境（关键，别踩坑）

- worktree：C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator\worktrees\timeline-pacing
- 分支：feat/timeline-pacing，基于 master 5bb5701 切出（隔离了主线其他 agent 的未提交改动）
- 用户名含单引号 h'h'h：bash 里 cd 必须双引号包整个路径，如 cd "C:/Users/h'h'h/..."。不带引号会被解析坏。
- Python：C:/Users/h'h'h/AppData/Local/Programs/Python/Python313/python.exe（有 sqlalchemy 2.0.35）
- 测试风格：独立脚本，不是 pytest——python tests/test_xxx.py，脚本自己 sys.exit
- LLM：智谱 glm-4-flash。起服务环境变量：
  LLM_PROVIDER=zhipu
  ZHIPU_API_KEY=319c9d6c73d2474d9622e12565420641.h4cClOgRrekKsxrf
  ZHIPU_MODEL=glm-4-flash
  EMBEDDING_PROVIDER=local
  DATABASE_URL=sqlite:///./tl_test.db
- uvicorn 改代码后必须重启（没开 --reload 时旧进程不重载，踩过坑：8150 旧进程导致「时间线没生效」假象）

## 二、已完成（已提交）

| commit | 内容 |
|---|---|
| d78b81f | docs：时间线规划器+节点数据结构细化设计（A+C） |
| e044f9e | docs：设计修订——修4处逻辑冲突+首轮范围收窄 |
| 2224a77 | §11.1 timeline.py 纯函数+测试（26断言） |
| 27b8dfb | §11.2 接入生成流程——按剧情点分段投喂 |
| 5de11f6 | §7.3 LLM 节点规划+交接文档 |
| 37fbee8 | §11.3 TimelineState 落库 Project.plot_timeline（5 改 + 1 新测试，51 断言） |
| c53275c | v2.1 exit_condition LLM 判定+force_complete_current（2 改 + 1 新测试，46 断言） |
| 497e0b2 | v2.2 节点粒度强化（prompts 剔除 plot_bible/premise，build_node_prompt 硬约束） |
| eb8e526 | v2.3 流式版时间线集成（generate_next_segment_stream + regenerate_segment_stream） |
| 6ea841f | v2.4 高潮密度蒸馏（Project.plot_rhythm + classify_chapter_emotion + distill_avg_node_segments） |
| 6c5774c | v2.5 支线滚动触发器（KG-driven，detect_kg_signal + should_roll + plan_branch） |
| 7b9a60f | v2.5 阶段 2/3（流式版补 v2.5 集成 + build_node_prompt 区分 main/branch） |
| 3914581 | v2.6 节点粒度彻底根治（stop token：【本节完】+ 副 marker） |

核心设计（详见 docs/timeline-pacing-design.md）：
- 根因：generate_next_segment 每段把整条 outline + plot_bible 的 synopsis/direction（含结局走向）一次性塞进 prompt，模型必然第一章讲完
- 解法：时间线模式（传 node_prompt）下不注入整条大纲、不注入 synopsis/direction，只注入当前剧情点；人物关系/伏笔保留（防漂移）。无 outline 走原逻辑（向后兼容）
- 首轮范围：只蒸 branch_ratio（高潮密度/长度、exit 的 LLM 判定都留 v2）
- 进度：双进度条（主线节点 done 比 + 支线额度段数比），分母都固定
- advance：段数预算驱动（写满 segment_budget 段即 done），纯函数可测

已建文件：
- backend/app/generation/timeline.py：PlotNode/TimelineState/plan_timeline/advance/timeline_progress/distill_branch_ratio/build_node_prompt + plan_nodes_with_llm（§7.3，刚加未提交）
- backend/tests/test_timeline.py：46 断言全过

## 三、当前断点

§11.3 已完成并提交（37fbee8），含 DB 持久化：
- Project.plot_timeline JSON 列（迁移清单已加，老库自动补列幂等）
- state_to_dict / state_from_dict 纯函数 + version 字段
- _timeline_state_for 走「存档恢复优先，无则首次规划+落库」双路径
- 服务重启后第一次 generate-plan 不再调 plan_nodes_with_llm
  （端到端验证：探针确认未触发，current_node 立即是已推进的节点）

无未提交改动。v2.5 阶段 2/3 已合到 master（5d0b0a8）。
下一个断点是节点粒度彻底根治（清单最后项），或等网络 push。

## 四、§11.4 端到端验证（已完成）

上轮（§7.3 提交后）验证结论：
- generate-plan 注入：节点已填实（n1=「入职立足」+ entry/exit 全有）
- generate 第一段：1212 字，**未完结**（核心 bug 已修）
- 弱点：第一段把"与顾辰逸相识相恋"（应属第 4 节点）也写进来了，节点粒度
  偏粗。是 §11.4 之外的语料/设计级调优，不阻塞主线推进。

§11.3 提交后追加验证（持久化）：
- 服务首次 generate-plan → plot_timeline 落库（3 节点 n1=入职立足/n2=危机四伏/n3=??）
- generate 走完 1 段后状态推进到 n2（n1 已 done，seg_counts={n1:1}）
- 杀服务，重启
- 第二次 generate-plan → current_node 立即是 n2，**未调 plan_nodes_with_llm**
  （用临时文件 marker 探针确认：marker 不存在 → LLM 未触发）

辅助脚本（scratch 目录，端口写死需改）：
- tl_genplan.json：{"project_id":1,"novel_ids":[1]}
- tl_proj.json：建项目 body（8段 outline）
- tl_check_plan2.py：验证 generate-plan 注入（端口 8151，要改）
- tl_gen_seg2.py：生成第一段+完结检测（端口 8151，要改）
- **tl_check_plan_8152.py / tl_gen_seg_8152.py**（端口 8152 的版本，§11.4 验证时落地）
- tl_inspect_db2.py：sqlite3 直读 tl_test.db 的 plot_timeline 存档（绕过 ORM 默认 novel_dev.db）
- tl_debug_orm.py：直接调 _timeline_state_for 验证存档恢复

## 五、后续待办

- **彻底根治节点粒度（最后攻破）**：v2.2 已是 prompt 工程极限（剔除全
  端到端验证：v2.1 真智谱下第一段不再演结局，judge 仍能识别 exit 达成。
  剩余边角：few-shot 风格样例仍会"顺手带出"未提及角色（如红楼梦薛姨妈
  混入现代言情）——这是语料副作用，不属时间线范围
- **v2.6 stop token 方向已实现**（3914581 提交到 master）：见清单"已完
  成"——prompt 措辞之外的根治靠 stop token，LLM 写【本节完】时停。B/C
  方案（JSON 结构化/工具调用）留作更彻底方案。
- 合并：完成后 feat/timeline-pacing 并回主线（用户说后面一起合并）

## 六、分支全局状态

- 本 worktree：feat/timeline-pacing，HEAD 7b9a60f（v2.5 阶段 2/3 提交），工作树干净
- master HEAD 3914581（v2.6 stop token + 之前所有合并）
- 另一 worktree worktrees/style-distill（feat/style-distillation）：风格蒸馏，独立，别动
- 主线 master 有其他 agent 的大量未提交改动（config/llm/generation/schemas/frontend），本分支从干净 5bb5701 切的，别去主线 worktree 操作
