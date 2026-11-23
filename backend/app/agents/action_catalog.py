"""助手可执行操作目录（单一真源）。

## 为什么需要这个模块

前端历史上把同一个能力分散成「按钮 + 页签 + 薄封装」三套入口。收敛方向是
**让助手自己发现可执行操作**，而不是继续在提示词里写死一份操作清单——提示词
会漂、会和后端路由脱节，而且新增能力时必须改两处。

本模块把「有哪些操作、谁能执行、要不要确认、现在有没有入口」声明成数据，
再由 `list_actions` 只读工具与 `/api/assistant/qa` 在运行时读取。模型看到的是
**本次问题命中的操作**，不是全量清单；提示词里只写「先查目录」，不写具体条目。

## 分级（与前端收敛方案一一对应）

| policy | 含义 | 前端入口 |
| --- | --- | --- |
| `auto_read` | 助手可直接调只读工具执行 | 可移除/降级 |
| `propose_write` | 助手只能提出，用户在卡片上点确认后才执行 | 保留必要确认 |
| `frontend_only` | 破坏性/批量/配置/密钥：必须在前端显式操作 | 必须保留 + 二次确认 |
| `unwired_read` | 只读且后端就绪，但工具与前端入口都还没有 | 助手如实说「暂未接入」 |

`ActionSpec.__post_init__` 把这张表当成约束来校验：档 3 的操作一旦被标成可
自动执行、或自动执行项没绑已注册的只读工具，模块导入就会失败（fail loud），
避免「声明很安全、实际很危险」的漂移。

## 边界

- 本模块**不**执行任何操作、不碰 db、不发 HTTP；它只描述与筛选。
- 触发词是通用词（能力名/意图词），不写死书名、人名、章节号。
- 端点字符串与 `backend/app/routers/*.py` 的装饰器保持一致；这里只是索引，
  真正的请求仍由既有端点处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.registry import ALLOWED_READ_ONLY_TOOLS

ActionCategory = Literal["read", "write", "destructive"]
ActionPolicy = Literal["auto_read", "propose_write", "frontend_only", "unwired_read"]

_CATEGORIES: frozenset[str] = frozenset({"read", "write", "destructive"})
_POLICIES: frozenset[str] = frozenset(
    {"auto_read", "propose_write", "frontend_only", "unwired_read"}
)

#: 策略 → 面向模型的一句话解释（拼进 prompt 时用，避免模型把「可提出」当「已执行」）。
_POLICY_HINTS: dict[str, str] = {
    "auto_read": "可直接执行",
    "propose_write": "只能提出，用户点确认卡片后才执行",
    "frontend_only": "只能由用户在前端页面操作，不得代执行",
    "unwired_read": "后端已就绪但尚未接入，回答时如实说明",
}

#: 同一问句命中多个操作时的排序：能直接做的排前面。
_POLICY_RANK: dict[str, int] = {
    "auto_read": 0,
    "propose_write": 1,
    "frontend_only": 2,
    "unwired_read": 3,
}


@dataclass(frozen=True)
class ActionSpec:
    """一条可被助手发现的操作声明。

    Attributes:
        key: 稳定标识（snake_case），用于日志与前端按 key 分派。
        title: 面向用户的一句话名称。
        summary: 能力边界说明（能做什么、不能做什么）。
        category: read / write / destructive。
        policy: 见模块 docstring 的四档。
        endpoint: 既有后端端点；auto_read 必须为空（走工具，不走 HTTP）。
        method: HTTP 方法，与 endpoint 同现。
        read_tool: `auto_read` 绑定的只读工具名，必须在白名单内。
        frontend_entry: 当前前端入口描述；`frontend_only` 时必须非空，
            这样助手能明确告诉用户「去哪个页面」。
        triggers: 命中词（通用能力词，不含书名/人名/章节号）。
        requires_confirmation: 用户是否必须显式确认。
        batch: 是否批量操作。
        touches_config: 是否改配置/密钥/成本参数。
    """

    key: str
    title: str
    summary: str
    category: ActionCategory
    policy: ActionPolicy
    endpoint: str | None = None
    method: str | None = None
    read_tool: str | None = None
    frontend_entry: str = ""
    triggers: tuple[str, ...] = field(default_factory=tuple)
    requires_confirmation: bool = False
    batch: bool = False
    touches_config: bool = False

    def __post_init__(self) -> None:
        if not self.key or not self.title:
            raise ValueError("ActionSpec.key / title 不能为空")
        if self.category not in _CATEGORIES:
            raise ValueError(f"未知 category {self.category!r}")
        if self.policy not in _POLICIES:
            raise ValueError(f"未知 policy {self.policy!r}")

        if self.policy == "auto_read":
            # 自动执行只允许只读，且必须绑一个真实注册过的只读工具。
            if self.category != "read":
                raise ValueError(f"{self.key}: auto_read 只能是 read")
            if self.read_tool not in ALLOWED_READ_ONLY_TOOLS:
                raise ValueError(
                    f"{self.key}: auto_read 必须绑定白名单内的只读工具，"
                    f"收到 {self.read_tool!r}"
                )
            if self.endpoint or self.method:
                raise ValueError(f"{self.key}: auto_read 走工具，不应再声明端点")
            if self.requires_confirmation or self.batch or self.touches_config:
                raise ValueError(
                    f"{self.key}: auto_read 不得要求确认/批量/改配置"
                )
        elif self.policy in ("propose_write", "frontend_only", "unwired_read"):
            if not self.endpoint or not self.method:
                raise ValueError(f"{self.key}: {self.policy} 必须声明 endpoint + method")
            if self.read_tool:
                raise ValueError(f"{self.key}: {self.policy} 不应绑定读工具")
        if self.policy == "unwired_read" and self.category != "read":
            raise ValueError(f"{self.key}: unwired_read 只能是 read")

        if self.category == "destructive":
            if self.policy != "frontend_only":
                raise ValueError(
                    f"{self.key}: 破坏性操作必须 frontend_only（前端显式入口 + 二次确认）"
                )
            if not self.requires_confirmation:
                raise ValueError(f"{self.key}: 破坏性操作必须 requires_confirmation=True")
        if self.policy == "frontend_only" and not self.frontend_entry:
            raise ValueError(f"{self.key}: frontend_only 必须写明前端入口")
        if self.policy == "frontend_only" and not self.requires_confirmation:
            raise ValueError(f"{self.key}: frontend_only 必须 requires_confirmation=True")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "category": self.category,
            "policy": self.policy,
            "policy_hint": _POLICY_HINTS[self.policy],
            "endpoint": self.endpoint,
            "method": self.method,
            "read_tool": self.read_tool,
            "frontend_entry": self.frontend_entry,
            "triggers": list(self.triggers),
            "requires_confirmation": self.requires_confirmation,
            "batch": self.batch,
            "touches_config": self.touches_config,
        }


# ====================================================================
# 目录
# ====================================================================
#
# 端点写法与 routers 装饰器一致；`{project_id}` 是路径参数占位，不是本机路径。

ACTIONS: tuple[ActionSpec, ...] = (
    # ---------- auto_read：助手已能直接做的只读操作 ----------
    ActionSpec(
        key="character_profile",
        title="查角色人设与关系",
        summary="按角色名返回人设与关系出边；只读，不写图谱。",
        category="read",
        policy="auto_read",
        read_tool="get_character_profile",
        triggers=("角色", "人物", "人设", "人物关系", "关系网"),
    ),
    ActionSpec(
        key="plot_reminders",
        title="查未填坑与角色约束",
        summary="返回未填坑清单与角色约束；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_reminders",
        triggers=("未填坑", "挖坑", "伏笔", "坑", "提醒"),
    ),
    ActionSpec(
        key="generation_review",
        title="读已生成段落",
        summary="读取本项目已生成的段（可按段号定向）；只读。",
        category="read",
        policy="auto_read",
        read_tool="read_generation_segments",
        triggers=("段落", "这段", "这一段", "上一段", "最新一段", "正文"),
    ),
    ActionSpec(
        key="corpus_search",
        title="检索参考语料",
        summary="对参考语料做混合检索；只读，不代表本项目正文。",
        category="read",
        policy="auto_read",
        read_tool="search_novel_chunks",
        triggers=("语料", "参考", "原文片段", "检索"),
    ),
    ActionSpec(
        key="corpus_audit",
        title="语料审核",
        summary="按空/超短/重复/注入串/缺画像/缺向量等规则只读体检语料。",
        category="read",
        policy="auto_read",
        read_tool="audit_corpus",
        triggers=("语料审核", "语料质量", "语料重复", "分片质量"),
    ),
    ActionSpec(
        key="kg_audit",
        title="图谱审核",
        summary="检查孤儿节点、角色名冲突、已死角色挂坑、稳定共现无边；只读。",
        category="read",
        policy="auto_read",
        read_tool="audit_kg",
        triggers=("图谱审核", "图谱质量", "关系遗漏", "孤儿节点"),
    ),
    ActionSpec(
        key="generation_audit",
        title="生成物审核",
        summary="按规则只读检查生成正文的一致性与重复等问题。",
        category="read",
        policy="auto_read",
        read_tool="audit_generation",
        triggers=("生成审核", "生成质量", "正文检查"),
    ),
    ActionSpec(
        key="log_analysis",
        title="日志分析",
        summary="按规则只读分析运行日志里的失败与异常模式。",
        category="read",
        policy="auto_read",
        read_tool="analyze_logs",
        triggers=("日志", "报错", "失败原因", "异常"),
    ),
    ActionSpec(
        key="bible_summary",
        title="读设定与大纲",
        summary="读取项目设定、前提与大纲原文，不做二次创作。",
        category="read",
        policy="auto_read",
        read_tool="summarize_bible",
        triggers=("设定", "大纲", "梗概", " premise"),
    ),
    ActionSpec(
        key="flavor_memory",
        title="查 AI 腔历史治法",
        summary="查询 AI 腔问题历史处理记录；未命中时必须说明无记录。",
        category="read",
        policy="auto_read",
        read_tool="query_ai_flavor_memory",
        triggers=("ai腔", "AI 腔", "机器味", "翻译腔"),
    ),
    ActionSpec(
        key="retrieval_trace",
        title="查检索轨迹",
        summary="查看最近检索的命中与空召回，判断检索是否跑偏；只读。",
        category="read",
        policy="auto_read",
        read_tool="inspect_retrieval_trace",
        triggers=("检索轨迹", "召回", "检索跑偏", "检索结果"),
    ),
    ActionSpec(
        key="catalog_browse",
        title="浏览作品目录",
        summary="浏览分卷/章节结构；只读。",
        category="read",
        policy="auto_read",
        read_tool="browse",
        triggers=("目录", "章节列表", "分卷", "有哪些章"),
    ),

    # ---------- propose_write：助手提出、用户点确认 ----------
    ActionSpec(
        key="invoke_generation",
        title="调动生成/改写单段",
        summary="对某一段发起生成或改写；会覆盖既有段落，必须用户确认。",
        category="write",
        policy="propose_write",
        endpoint="/api/assistant/invoke-generation",
        method="POST",
        triggers=("重写", "改写", "续写", "再写一段", "重生成"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="create_proposal",
        title="创建编辑提案",
        summary="把用户自己敲定的改法落成提案；助手不代写正文，需用户确认创建。",
        category="write",
        policy="propose_write",
        endpoint="/api/assistant/proposals",
        method="POST",
        triggers=("提案", "改成", "替换成", "建议修改"),
        requires_confirmation=True,
    ),

    # ---------- frontend_only：破坏性 / 批量 / 配置 / 密钥 ----------
    ActionSpec(
        key="snapshot_rollback",
        title="快照回滚",
        summary="把项目回滚到某个快照；不可撤销。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/snapshots/rollback",
        method="POST",
        frontend_entry="项目页步骤 6「重写快照与回滚」→ 按段列表的「回滚到这条」",
        triggers=("回滚", "恢复到快照", "撤销到之前"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="snapshot_rollback_group",
        title="分组批量回滚",
        summary="按分组批量回滚多个快照；不可撤销。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/snapshots/rollback-group",
        method="POST",
        frontend_entry="项目页步骤 6「重写快照与回滚」→ 按整组操作列表的「整组回滚」",
        triggers=("批量回滚", "回滚一组"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="version_restore",
        title="版本回滚",
        summary="恢复某个历史版本；会覆盖当前内容。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/versions/{version_id}/restore",
        method="POST",
        frontend_entry="版本恢复入口（当前仅后端有，前端未接）",
        triggers=("版本回滚", "恢复版本", "历史版本"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="proposal_apply",
        title="应用编辑提案",
        summary="把已通过的提案写进正文。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/proposals/{proposal_id}/apply",
        method="POST",
        frontend_entry="项目页步骤 6「待应用提案」→ 已批准提案行内「应用」（单条）",
        triggers=("应用提案", "落盘提案"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="proposal_apply_selected",
        title="批量应用提案",
        summary="一次应用多条提案；批量改正文。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/proposals/apply-selected",
        method="POST",
        frontend_entry="项目页步骤 6「待应用提案」→ 勾选后「应用选中」",
        triggers=("批量应用", "全部应用"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="clean_corpus",
        title="语料清洗",
        summary="批量清洗语料库；会改动语料数据。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/assistant/clean-corpus",
        method="POST",
        frontend_entry="语料页「语料清洗」卡片 → 扫描并复核（不写正文，只产 pending 提案）",
        triggers=("清洗语料", "语料清洗"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="kg_rebuild",
        title="重建图谱",
        summary="按已生成正文批量重建图谱；耗时且覆盖现图。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/build-kg-from-generations",
        method="POST",
        frontend_entry="项目页⑦「建/重建图谱」按钮",
        triggers=("重建图谱", "重新建图", "图谱重建"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="narrative_rebuild",
        title="重建叙事层",
        summary="批量重建叙事层数据。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/build-narrative",
        method="POST",
        frontend_entry="项目页叙事层入口（当前未接）",
        triggers=("重建叙事", "叙事层重建"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="kg_worker_requeue",
        title="批抽任务重排",
        summary="把失败的批抽章节重新入队；批量。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/kg-v3/worker/requeue",
        method="POST",
        frontend_entry="KG v3 工作台批抽面板",
        triggers=("批抽重排", "重新入队", "重排队列"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="kg_worker_recover",
        title="批抽任务恢复",
        summary="恢复中断的批抽任务；批量。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/kg-v3/worker/recover",
        method="POST",
        frontend_entry="KG v3 工作台批抽面板（当前未接）",
        triggers=("批抽恢复", "恢复任务"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="kg_worker_backfill",
        title="批抽回填",
        summary="对历史章节批量回填抽取结果。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/kg-v3/worker/backfill",
        method="POST",
        frontend_entry="KG v3 工作台批抽面板（当前未接）",
        triggers=("批抽回填", "历史回填"),
        requires_confirmation=True,
        batch=True,
    ),
    ActionSpec(
        key="kg_exemption_delete",
        title="删除图谱豁免",
        summary="删除一条豁免规则；会影响后续判定。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/kg-v3/exemptions/{exemption_id}",
        method="DELETE",
        frontend_entry="KG v3 工作台豁免列表的删除按钮",
        triggers=("删除豁免", "去掉豁免"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="bible_character_remove",
        title="删除角色",
        summary="从设定表删除角色；不可撤销（需走版本恢复）。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/bible/characters/remove",
        method="POST",
        frontend_entry="设定面板角色行的删除按钮",
        triggers=("删除角色", "移除角色"),
        requires_confirmation=True,
    ),
    ActionSpec(
        key="llm_config_save",
        title="保存模型配置",
        summary="写入模型供应商地址与密钥；涉及凭证。",
        category="write",
        policy="frontend_only",
        endpoint="/api/settings/llm",
        method="PUT",
        frontend_entry="设置页 LLM 表单",
        triggers=("改模型", "切换模型", "配置密钥", "api key"),
        requires_confirmation=True,
        touches_config=True,
    ),
    ActionSpec(
        key="distill_start",
        title="启动文风提炼",
        summary="启动或续跑文风提炼；耗时且消耗额度。",
        category="write",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/distill",
        method="POST",
        frontend_entry="项目页文风档位面板的启动按钮",
        triggers=("提炼", "蒸馏", "文风提炼", "文风蒸馏", "风格提炼", "风格蒸馏"),
        requires_confirmation=True,
        touches_config=True,
    ),
    ActionSpec(
        key="distill_apply",
        title="应用提炼风格",
        summary="把某份提炼结果应用到项目；改生成配置。",
        category="write",
        policy="frontend_only",
        endpoint="/api/projects/{project_id}/style-distills/apply",
        method="POST",
        frontend_entry="项目页文风档案库的「应用」按钮",
        triggers=("应用风格", "应用提炼", "应用蒸馏"),
        requires_confirmation=True,
        touches_config=True,
    ),
    ActionSpec(
        key="generate_loop",
        title="多段循环生成",
        summary="批量连续生成多段；成本高，前端当前无入口。",
        category="write",
        policy="frontend_only",
        endpoint="/api/generation/generate-loop",
        method="POST",
        frontend_entry="项目页生成面板（当前未接循环生成）",
        triggers=("连续生成", "批量生成", "多章生成"),
        requires_confirmation=True,
        batch=True,
        touches_config=True,
    ),
    ActionSpec(
        key="novel_seed",
        title="播种演示语料",
        summary="写入演示语料，仅供开发/演示；会造数据。",
        category="destructive",
        policy="frontend_only",
        endpoint="/api/novels/seed",
        method="POST",
        frontend_entry="不开放前端入口（仅开发用）",
        triggers=("播种语料", "演示数据"),
        requires_confirmation=True,
        batch=True,
    ),

    # ---------- 诊断 / 运行记录 / 提案 / 版本 / 提示词 / 快照（2026-11-21 接线）----------
    ActionSpec(
        key="project_diagnosis",
        title="项目诊断",
        summary="规则先行的项目体检（未填坑/别名冲突/哨兵/无正文段/文风偏离）；只读，不调模型。",
        category="read",
        policy="auto_read",
        read_tool="run_diagnosis",
        triggers=("诊断", "体检", "哪里有问题"),
    ),
    ActionSpec(
        key="diagnostic_skills",
        title="诊断技能目录",
        summary="列出可用诊断技能、版本、实验状态与授权要求；列目录不授权执行。",
        category="read",
        policy="auto_read",
        read_tool="list_diagnostic_skills",
        triggers=("诊断技能", "有哪些诊断", "技能目录"),
    ),
    ActionSpec(
        key="diagnostic_task",
        title="深度诊断任务",
        summary="按技能跑独立诊断（前情/时间线/绑定/闭集/正文一致性/提示词体检等）；实验技能需用户原话授权。",
        category="read",
        policy="auto_read",
        read_tool="run_diagnostic_task",
        triggers=("深度诊断", "实验性诊断", "一致性核对", "闭集盘点", "提示词体检", "修复规划"),
    ),
    ActionSpec(
        key="body_repair_card",
        title="定点修订卡",
        summary="按诊断出的矛盾构造局部修订待确认卡；只读，不写正文、不落库。",
        category="read",
        policy="auto_read",
        read_tool="prepare_body_repair",
        triggers=("定点修复", "局部修订", "修这一段", "局部改"),
    ),
    ActionSpec(
        key="assistant_runs",
        title="助手运行记录",
        summary="查看历次助手调用的状态与耗时；只读，不新开 run。",
        category="read",
        policy="auto_read",
        read_tool="list_assistant_runs",
        triggers=("运行记录", "调用记录", "助手历史"),
    ),
    ActionSpec(
        key="proposal_list",
        title="提案列表",
        summary="查看编辑提案及其状态；只读，不审批、不应用。",
        category="read",
        policy="auto_read",
        read_tool="list_proposals",
        triggers=("提案列表", "有哪些提案"),
    ),
    ActionSpec(
        key="version_list",
        title="版本历史",
        summary="查看版本快照列表；只读，恢复旧版本需用户在前端确认。",
        category="read",
        policy="auto_read",
        read_tool="list_versions",
        triggers=("版本历史", "版本差异", "diff"),
    ),
    ActionSpec(
        key="prompt_active",
        title="当前提示词",
        summary="查看当前生效提示词的来源（运行时覆写/代码默认）；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_active_prompts",
        triggers=("提示词", "prompt"),
    ),
    ActionSpec(
        key="snapshot_list",
        title="快照列表",
        summary="查看可回滚的正文快照；只读，回滚本身需用户在前端确认。",
        category="read",
        policy="auto_read",
        read_tool="list_snapshots",
        triggers=("快照", "备份点"),
    ),
    ActionSpec(
        key="kg_worldview",
        title="图谱世界观",
        summary="查看世界观维度：势力/地点/体系境界/资源规则，各带证据章号；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_kg_worldview",
        triggers=("世界观", "设定体系"),
    ),
    ActionSpec(
        key="kg_timeline",
        title="图谱时间线",
        summary="查看按章推进的事件时间线（谁/在哪/做了什么/结果）；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_kg_timeline",
        triggers=("时间线", "时间轴"),
    ),
    ActionSpec(
        key="kg_story_context",
        title="故事上下文",
        summary="按「写到第几章」截取前情、未回收伏笔与关键人物状态；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_story_context",
        triggers=("故事上下文", "上下文"),
    ),
    ActionSpec(
        key="text_gate",
        title="无生成章检查",
        summary="查 KG 提取时「该段没有对应正文」的累计次数（按段去重、带告警阈值）；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_text_gate_no_generation",
        triggers=("没有正文", "缺正文", "未生成章节"),
    ),
    ActionSpec(
        key="style_distill_detail",
        title="提炼详情",
        summary="查看单份提炼档案的档位/章数/规则数与文风全文；只读。",
        category="read",
        policy="auto_read",
        read_tool="get_style_distill_detail",
        triggers=("提炼详情", "提炼结果", "蒸馏详情", "蒸馏结果"),
    ),
    # ---------- 附件（批 10 附件入口接线）----------
    ActionSpec(
        key="read_attachment",
        title="读文本附件",
        summary="分段读取项目里暂存的文本附件；只读，内容不可信。",
        category="read",
        policy="auto_read",
        read_tool="read_attachment",
        triggers=("附件", "这份文件", "这个文件", "txt", "文档"),
    ),
    ActionSpec(
        key="preview_attachment_cleaning",
        title="预览附件清洗",
        summary="预览附件投放前的清洗差异与混书体检；只读，不写库。",
        category="read",
        policy="auto_read",
        read_tool="preview_attachment_cleaning",
        triggers=("会洗掉", "清洗预览", "清洗差异", "预处理"),
    ),
    ActionSpec(
        key="check_attachment_cleaning",
        title="复核附件清洗",
        summary="确定性全量残留 + 抽样模型复核；只读，blocked 时不可投放。",
        category="read",
        policy="auto_read",
        read_tool="check_attachment_cleaning",
        triggers=("检查清洗", "复核", "能不能投放", "质量检查"),
    ),
    # ---------- 本书素材包（只读）----------
    ActionSpec(
        key="material_pack_toc",
        title="看本书素材包目录",
        summary="列出素材包的分类、年份、用途与最近入库标题；只读，不触发调研。",
        category="read",
        policy="auto_read",
        read_tool="material_pack_toc",
        triggers=("素材包", "资料库", "素材目录", "有没有资料", "资料收集"),
    ),
    ActionSpec(
        key="material_pack_search",
        title="检索本书素材包",
        summary="按关键词查可用素材，返回年份、分类与适用处；只读。",
        category="read",
        policy="auto_read",
        read_tool="material_pack_search",
        triggers=("查素材", "找资料", "素材里有没有", "参考资料", "背景资料"),
    ),
    ActionSpec(
        key="material_pack_get",
        title="读取素材内容",
        summary="按素材 id 取具体摘要与用途；只读。",
        category="read",
        policy="auto_read",
        read_tool="material_pack_get",
        triggers=("素材详情", "这条素材", "素材内容", "打开素材"),
    ),
    ActionSpec(
        key="material_pack_by_use",
        title="按用途找素材",
        summary="按「可用在什么地方」检索素材；只读。",
        category="read",
        policy="auto_read",
        read_tool="material_pack_by_use",
        triggers=("用什么素材", "适合什么场景", "章节素材", "用途"),
    ),
)


# ====================================================================
# 查询 / 匹配
# ====================================================================


def model_tool_names() -> tuple[str, ...]:
    """**唯一**的「哪些工具可以给模型看」真源。

    规则：
      - 只取 `auto_read` 绑定的 `read_tool`；
      - 以及 `unwired_read` 里**确实已注册成只读工具**的那些（目前没有：
        它们的 `read_tool` 一律为 None，只有 endpoint）；
      - 必须同时出现在注册表白名单 `ALLOWED_READ_ONLY_TOOLS` 里。

    档 2（`propose_write`）与档 3（`frontend_only`）**永不**进入本集合：
    它们没有 `read_tool`，也不是注册表里的工具。这样「模型能调用什么」
    与「用户必须确认什么」在数据层面就是两个不相交的集合。
    """
    from app.agents.registry import ALLOWED_READ_ONLY_TOOLS

    names: list[str] = []
    for spec in ACTIONS:
        if spec.policy not in ("auto_read", "unwired_read"):
            continue
        if not spec.read_tool or spec.read_tool not in ALLOWED_READ_ONLY_TOOLS:
            continue
        if spec.read_tool not in names:
            names.append(spec.read_tool)
    return tuple(names)


def _iter_actions(
    *, category: str | None = None, policy: str | None = None
) -> list[ActionSpec]:
    if category is not None and category not in _CATEGORIES:
        raise ValueError(f"未知 category {category!r}")
    if policy is not None and policy not in _POLICIES:
        raise ValueError(f"未知 policy {policy!r}")
    return [
        spec
        for spec in ACTIONS
        if (category is None or spec.category == category)
        and (policy is None or spec.policy == policy)
    ]


def list_actions(
    *,
    query: str | None = None,
    category: str | None = None,
    policy: str | None = None,
) -> list[dict[str, Any]]:
    """列出操作目录（可按 query / category / policy 过滤）。

    `query` 非空时复用 `match_actions` 的命中逻辑；为空则返回目录全集。
    """
    if query and query.strip():
        specs = match_actions(query, limit=len(ACTIONS))
        if category:
            specs = [s for s in specs if s.category == category]
        if policy:
            specs = [s for s in specs if s.policy == policy]
    else:
        specs = _iter_actions(category=category, policy=policy)
    return [spec.to_dict() for spec in specs]


def match_actions(query: str, *, limit: int = 4) -> list[ActionSpec]:
    """按用户问句里的能力词匹配操作。

    确定性匹配、不调模型：宁可漏也不误提——目录只用于让模型知道「能不能做、
    在哪做」，误命中不会执行任何写操作。命中的触发词越长，得分越高。
    """
    text = (query or "").strip().lower()
    if not text:
        return []

    scored: list[tuple[int, int, ActionSpec]] = []
    for spec in ACTIONS:
        hits = [t for t in spec.triggers if t and t.lower() in text]
        if not hits:
            continue
        score = sum(len(t) for t in hits)
        scored.append((score, -_POLICY_RANK[spec.policy], spec))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [spec for _, _, spec in scored[: max(1, limit)]]


def actions_prompt_block(query: str, *, limit: int = 4) -> str:
    """把命中的操作渲染成一段上下文；未命中返回空串。

    只列**本次问题命中**的操作，并明确四档策略——避免模型把
    `propose_write` 当成已执行、把 `frontend_only` 当成可以代劳。
    """
    matched = match_actions(query, limit=limit)
    if not matched:
        return ""

    lines = [
        "可执行操作（运行时来自后端动作目录；目录外的一律视为不支持，不要承诺）："
    ]
    for spec in matched:
        hint = _POLICY_HINTS[spec.policy]
        entry = (
            f"；入口：{spec.frontend_entry}"
            if spec.policy == "frontend_only" and spec.frontend_entry
            else ""
        )
        lines.append(f"- {spec.title}｜{spec.category}｜{hint}{entry}：{spec.summary}")
    return "\n".join(lines)
