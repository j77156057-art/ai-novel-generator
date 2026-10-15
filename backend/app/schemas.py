from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


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

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: int
    novel_id: int
    chapter_no: Optional[int]
    idx: int
    text: str

    model_config = {"from_attributes": True}


class UploadResult(BaseModel):
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
    generation_length: int = 3000


class ProjectPatch(StrictRequest):
    """向导分步更新项目字段——每次只传要改的字段。"""

    title: Optional[str] = None
    target_length: Optional[int] = None
    mode: Optional[str] = None
    genre: Optional[str] = None
    style: Optional[str] = None
    premise: Optional[str] = None
    reference_novel_ids: Optional[list[int]] = None
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
    plot_bible: Optional[dict] = None
    generation_length: int = 3000
    segment_count: int = 0
    # 叙事蒸馏档位（项目级偏好，空=默认完整档）
    narrative_tier: Optional[str] = None
    # 已落库文风档案（蒸馏库「应用」写入的单槽缓存，生成链路直接读它）。
    # 列表也带它：项目数不多、单条档案体量可控，且前端要据此反映「已应用」状态。
    style_narrative: Optional[dict] = None

    model_config = {"from_attributes": True}


class PlotBibleOut(BaseModel):
    characters: list[dict] = []
    relationships: list[dict] = []
    direction: Optional[str] = None  # 后续走向
    conflicts: list[str] = []  # 剧情冲突
    foreshadowing: list[str] = []  # 伏笔
    synopsis: Optional[str] = None  # 完整剧情梗概（quick/expert 构思结果）
    note: Optional[str] = None  # 降级提示（如模型限流时返回本地模板并说明）


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
