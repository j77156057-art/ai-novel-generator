from datetime import datetime
from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, StringConstraints


class StrictRequest(BaseModel):
    """**请求体**基类：未知字段一律 422，绝不静默丢弃。

    动机（实测踩过）：`novel_id` 改名为 `novel_ids` 后，若调用方仍提交 `novel_id`，
    pydantic 默认的 `extra="ignore"` 会**静默丢掉**它——接口照样返回 200、一切看起来正常，
    但参考语料根本没生效。README 里的 curl 示例就中过这个坑（已修）。这类"静默无效"
    是排查成本最高的一种故障：没有报错、没有日志、只有结果不对。

    因此所有**请求体**都继承本类，拼错字段直接 fail loud。
    响应模型不继承：出参由我们自己构造，不存在拼写漂移，没必要为此牺牲向后兼容。
    """

    model_config = ConfigDict(extra="forbid")


class NovelOut(BaseModel):
    id: int
    title: Optional[str]
    author: Optional[str]
    platform: Optional[str]
    genre: Optional[str]
    status: Optional[str]
    # 导入时的语料清洗报告（第 8 步）。用户要能看到「洗掉了什么」：
    # 检出串列表 / 各串次数 / 剔除总字数 / 清洗前后字数 / 原文样本。
    clean_report: Optional[dict] = None
    # 这本语料**最大的章节号**（0 = 还没有切分出任何章节）。
    #
    # 为什么要有：界面上的「原文边界」要回答「故事进行到第几章」，
    # 用户得先知道这本书一共有多少章 —— 没有这个数，输入框就没有上界，
    # 只能靠用户自己猜，或者等后端 400 才告诉他「章节不存在」。
    max_chapter_no: int = 0

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: int
    novel_id: int
    chapter_no: Optional[int]
    idx: int
    text: str

    model_config = {"from_attributes": True}


class UploadResult(BaseModel):
    check_status: str = "review_required"
    novel_id: int
    title: Optional[str]
    chapters: int
    chunks: int
    # 导入时的清洗报告（第 8 步）：上传响应里随即回显，用户立刻能看到洗掉了什么。
    clean_report: Optional[dict] = None
    # 实际检出的文件编码（utf-8-sig / utf-8 / gb18030）；seed 等内部路径可为空。
    detected_encoding: Optional[str] = None


class SearchHit(BaseModel):
    chunk_id: int
    chapter_no: Optional[int]
    idx: int
    score: float
    bm25: float
    vector: float
    text: str

    model_config = {"from_attributes": True}


class ProjectCreate(StrictRequest):
    title: str
    target_length: int = 5000
    mode: str = "quick"  # quick | expert
    style_dims: Optional[dict] = None
    outline: Optional[Any] = None
    # 向导字段（可在创建时一并给，也可后续 PATCH）
    genre: Optional[str] = None
    style: Optional[str] = None
    premise: Optional[str] = None
    reference_novel_ids: Optional[list[int]] = None
    continuation_start_chapter: Optional[int] = None
    story_authority: Optional[str] = "generated_branch"
    reference_cutoffs: Optional[list[dict]] = None
    generation_length: int = 3000


class ProjectPatch(StrictRequest):
    """向导分步更新项目字段——每次只传要改的字段。"""

    title: Optional[str] = None
    outline: list[Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]] | None = None
    target_length: Optional[int] = None
    mode: Optional[str] = None
    genre: Optional[str] = None
    style: Optional[str] = None
    premise: Optional[str] = None
    reference_novel_ids: Optional[list[int]] = None
    continuation_start_chapter: Optional[int] = None
    story_authority: Optional[str] = "generated_branch"
    reference_cutoffs: Optional[list[dict]] = None
    generation_length: Optional[int] = None
    plot_bible: Optional[dict] = None
    status: Optional[str] = None
    # 叙事蒸馏档位（full/balanced/fast）。校验在 router 层做（要回 400 而不是 422），
    # 见 app.style.narrative.resolve_tier。
    narrative_tier: Optional[str] = None


class ProjectOut(BaseModel):
    id: int
    title: str
    target_length: int
    mode: str
    status: Optional[str]
    style_dims: Optional[dict] = None
    outline: Optional[Any] = None
    created_at: Optional[datetime]
    # 向导字段
    genre: Optional[str] = None
    style: Optional[str] = None
    premise: Optional[str] = None
    reference_novel_ids: Optional[list[int]] = None
    continuation_start_chapter: Optional[int] = None
    story_authority: Optional[str] = "generated_branch"
    reference_cutoffs: Optional[list[dict]] = None
    plot_bible: Optional[dict] = None
    generation_length: int = 3000
    segment_count: int = 0
    plot_timeline: Optional[dict] = None
    # 叙事蒸馏档位（项目级偏好，空=默认完整档）
    narrative_tier: Optional[str] = None
    # 已落库文风档案（蒸馏库「应用」写入的单槽缓存，生成链路直接读它）。
    # 列表也带它：项目数不多、单条档案体量可控，且前端要据此反映「已应用」状态。
    style_narrative: Optional[dict] = None

    model_config = {"from_attributes": True}


class PlotBibleOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    model_participated: bool = False
    degraded: bool = False
    status: str = "needs_planning"
    error: Optional[dict] = None
    characters: list[dict] = []
    relationships: list[dict] = []
    direction: Optional[str] = None  # 后续走向
    conflicts: list[str] = []  # 剧情冲突
    foreshadowing: list[str] = []  # 伏笔
    synopsis: Optional[str] = None  # 完整剧情梗概（quick/expert 构思结果）
    note: Optional[str] = None  # 降级提示（如模型限流时返回本地模板并说明）
    # 剧情结构参考 + 照搬检测：学结构可以，抄具体情节不行（机制级校验，不是提示层约束）。
    plot_structure_note: Optional[str] = None
    structure_copy_check: Optional[dict] = None
    # 世界背景与硬规则（作者手写）：一句话世界 / 硬规则 / 势力与地点。
    #
    # **必须显式声明**（2026-10-30）：PlotBibleOut 走 pydantic，
    # 未声明的字段会被 model_dump() **静默丢弃** —— 模型给了 background
    # 也会在落库前消失，而且不报错（与 StrictRequest 那条同源的坑）。
    background: Optional[dict] = None


class PremiseProposalOut(BaseModel):
    proposals: list[str] = []  # 大模型给出的若干「大概内容」方案
    note: Optional[str] = None  # 降级提示（如模型限流时返回本地模板并说明）


class ExpertQuestionOut(BaseModel):
    questions: list[str] = []  # wayfinder 追问清单
    note: Optional[str] = None


class GenerationOut(BaseModel):
    id: int
    project_id: int
    segment_no: int
    chapter_range: Optional[str]
    text: Optional[str]
    summary: Optional[Any]

    model_config = {"from_attributes": True}
