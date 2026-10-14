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
