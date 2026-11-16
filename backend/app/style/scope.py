"""蒸馏范围轴（scope）：让用户决定「这一轮蒸哪几章」。

## 为什么要有这条轴

原来「蒸多少」只有两个选择，都写在档位里：完整档按语料量百分比采样，
平衡/快速档固定 30 章。用户拿到的是**黑盒**——想要「只蒸第 1~50 章」
或「蒸整本」都做不到，只能去改代码常量。

而「蒸多少」直接决定三件事：

1. **成本**：833 章完整档实测 42 分钟（30% 采样）⇒ 全量是 2 小时量级；
2. **产出**：范围变了，档案的内容就变了 ⇒ 缓存键必须跟着变；
3. **可见性**：蒸馏读的是参考语料原文，而检索链读的是同一批原文 ——
   两边的可见边界必须一致，否则「检索得到的片段」和「蒸出来的规则」
   可能来自不同范围（见 `respect_cutoff`）。

## 设计上的三条取舍

1. **档位不变、范围另立轴**：档位（full/balanced/fast）管的是「提炼得多细」
   （维度白名单、角色名归一化、清洗强度），范围管的是「读哪几章」。
   两者正交，所以范围**不进档位表**，而是请求里的一个可选对象。
2. **默认不动**：不传 scope ⇒ 行为与今天逐字节一致（`mode="tier"`）。
   这样已有的项目、已有的缓存、已有的测试都不受影响。
3. **非法参数当场报错**：范围写错（`from > to`、`chapters=0`、未知 mode）
   必须在这里 400，而不是变成一个跑了两小时才失败的后台任务。

## 早期文风漂移：为什么不靠「悄悄丢掉早期章节」解决

采样器一直刻意避开早期章节（`_sample_chapters_fixed` 取后 2/3），
因为长篇作者的文风是演化的，早期写法不代表成熟期。

但「全量」档的口号就是**全量** —— 如果采样器在背后偷偷丢掉前 1/3，
那用户点「全量」得到的就是「后 2/3」，而且界面上看不出来。
⇒ 本设计取「**按章加权**」：早期章节**照样蒸馏、照样入库**，
但每条规则带一个成熟度权重（早期 0.5 / 成熟期 1.0），
注入提示词时按权重分层挑选（成熟期优先，同层内均匀摊开）。

这样既不骗用户（档案里确实有早期规则），也不让生涩的早期写法
主导提示词（成熟期权重更高、先进）。权重口径见
`app.style.narrative.maturity_weight`。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: 沿用档位自带的采样策略（不传 scope 时的默认，也是 `mode="tier"`）。
MODE_TIER = "tier"
#: 按语料量百分比采样（与完整档同一套「对话密集优先 + 场景均衡」策略）。
MODE_PERCENTAGE = "percentage"
#: 固定章数，**均匀摊在全书**（含早期章节 —— 早期漂移由权重处理，不靠偷偷丢章）。
MODE_FIXED = "fixed_chapters"
#: 章节区间 [from_chapter, to_chapter]，含两端。
MODE_RANGE = "range"
#: 全部章节。
MODE_ALL = "all"

#: 全部合法 mode（前端下拉与后端校验共用这一份）。
SCOPE_MODES: tuple[str, ...] = (
    MODE_TIER,
    MODE_PERCENTAGE,
    MODE_FIXED,
    MODE_RANGE,
    MODE_ALL,
)

#: 用户可能写成的样子 → 规范 mode。别名只收这两个常见的，
#: 多收几个等于「接口随笔者的习惯变形」，那是最难查的一类问题。
_MODE_ALIASES: dict[str, str] = {
    "tier": MODE_TIER,
    "default": MODE_TIER,
    "percentage": MODE_PERCENTAGE,
    "percent": MODE_PERCENTAGE,
    "fixed": MODE_FIXED,
    "fixed_chapters": MODE_FIXED,
    "range": MODE_RANGE,
    "all": MODE_ALL,
}

#: 各 mode 允许携带的参数（其余参数一律报错，不静默忽略）。
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    MODE_TIER: frozenset(),
    MODE_PERCENTAGE: frozenset({"ratio"}),
    MODE_FIXED: frozenset({"chapters"}),
    MODE_RANGE: frozenset({"from_chapter", "to_chapter"}),
    MODE_ALL: frozenset(),
}

#: 通用参数（任何 mode 都可带）。
_COMMON_PARAMS = frozenset({"mode", "respect_cutoff"})


@dataclass(frozen=True)
class DistillScope:
    """一次蒸馏的**范围**（与档位正交）。

    Attributes:
        mode: 见 `SCOPE_MODES`。
        ratio: `mode=percentage` 时的采样比例（0 < ratio ≤ 1）。
        chapters: `mode=fixed_chapters` 时的章数（≥ 1）。
        from_chapter / to_chapter: `mode=range` 时的区间（含两端，≥ 1）。
        respect_cutoff: 是否尊重项目的 `reference_cutoffs`（默认 True）。

    ## `respect_cutoff` 的口径（**不是** fail-closed）

    True 时：**这本书设了边界就只蒸边界之前**；没设边界就**整本照蒸**。

    这里与 `roleplay/evidence.py` 的 fail-closed **刻意不同**：
    角色聊天里没有边界就等于「整本不可见」，因为那是**剧透**问题；
    而蒸馏是**学写法**，参考书整本都是合法的风格材料
    （与 `generation/profiles.py` 的 `STYLE_REFERENCE_PATHS` 一致）。
    把剧透那套口径搬过来会让「没设边界的老项目」一蒸馏就变成空档案。

    设了边界的书仍按边界截断，是为了与检索链对齐 ——
    检索读到的片段与蒸馏出的规则必须来自同一个可见范围。
    """

    mode: str = MODE_TIER
    ratio: float | None = None
    chapters: int | None = None
    from_chapter: int | None = None
    to_chapter: int | None = None
    respect_cutoff: bool = True

    @property
    def is_default(self) -> bool:
        """是否等于「什么都没指定」（⇒ 行为与加范围轴之前逐字节一致）。"""
        return self.mode == MODE_TIER and bool(self.respect_cutoff) is True

    def fingerprint_payload(self) -> dict[str, Any]:
        """进入指纹的**有效配置**。

        范围变了 ⇒ 产出变了 ⇒ 旧缓存必须失效。这是「档位不进指纹」那个坑的
        同构问题：项目已踩过两次「换了输入却拿到旧结果且不报错」。
        """
        return {
            "mode": self.mode,
            "ratio": self.ratio,
            "chapters": self.chapters,
            "from_chapter": self.from_chapter,
            "to_chapter": self.to_chapter,
            "respect_cutoff": bool(self.respect_cutoff),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """给 API / 前端展示用的形状。"""
        return dict(self.fingerprint_payload())

    def signature(self) -> str:
        """范围的稳定短哈希（用于日志、任务去重、缓存键）。"""
        blob = json.dumps(self.fingerprint_payload(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def parse_scope(raw: Any) -> DistillScope:
    """把请求体里的 `scope` 解析成 `DistillScope`。

    Args:
        raw: `None` / 空 dict ⇒ 默认范围（`mode="tier"`）；
            否则是一个 dict，键为 `mode` / `ratio` / `chapters` /
            `from_chapter` / `to_chapter` / `respect_cutoff`。

    Returns:
        校验通过的 `DistillScope`。

    Raises:
        ValueError: 未知 mode、缺参数、多参数、参数越界。
            **在这里报错**（⇒ API 层 400），而不是让它变成一个跑了两小时
            才失败的后台任务。
    """
    if raw is None:
        return DistillScope()
    if not isinstance(raw, dict):
        raise ValueError("scope 必须是对象")

    unknown = set(raw) - _COMMON_PARAMS - set().union(*_ALLOWED_PARAMS.values())
    if unknown:
        allowed = "、".join(sorted(_COMMON_PARAMS | set().union(*_ALLOWED_PARAMS.values())))
        raise ValueError(
            f"scope 含未知字段：{'、'.join(sorted(unknown))}；可用字段：{allowed}"
        )

    # None 一律当「没给」：公开形状（`to_public_dict`）会把所有参数都列出来，
    # 值是 None 表示这个模式用不上它 —— 不是「传了一个非法参数」。
    provided = {k: v for k, v in raw.items() if v is not None}

    raw_mode = str(raw.get("mode") or MODE_TIER).strip().lower()
    mode = _MODE_ALIASES.get(raw_mode)
    if mode is None:
        raise ValueError(
            f"scope.mode={raw_mode!r} 不认识；可选：{'、'.join(SCOPE_MODES)}"
        )

    extra = set(provided) - _COMMON_PARAMS - _ALLOWED_PARAMS[mode]
    if extra:
        raise ValueError(
            f"scope.mode={mode} 不接受参数：{'、'.join(sorted(extra))}"
            f"（该模式可用：{'、'.join(sorted(_ALLOWED_PARAMS[mode])) or '无'}）"
        )

    respect_cutoff = provided.get("respect_cutoff", True)
    if not isinstance(respect_cutoff, bool):
        raise ValueError("scope.respect_cutoff 必须是布尔值")

    ratio: float | None = None
    chapters: int | None = None
    from_chapter: int | None = None
    to_chapter: int | None = None

    if mode == MODE_PERCENTAGE:
        ratio = provided.get("ratio")
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool):
            raise ValueError("scope.ratio 必须是数字（mode=percentage 时必填）")
        ratio = float(ratio)
        if not (0 < ratio <= 1):
            raise ValueError(f"scope.ratio={ratio} 必须在 (0, 1] 之间")
    elif mode == MODE_FIXED:
        chapters = provided.get("chapters")
        if not isinstance(chapters, int) or isinstance(chapters, bool):
            raise ValueError("scope.chapters 必须是整数（mode=fixed_chapters 时必填）")
        if chapters < 1:
            raise ValueError(f"scope.chapters={chapters} 必须 ≥ 1")
    elif mode == MODE_RANGE:
        from_chapter = provided.get("from_chapter")
        to_chapter = provided.get("to_chapter")
        for name, value in (("from_chapter", from_chapter), ("to_chapter", to_chapter)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"scope.{name} 必须是整数（mode=range 时必填）")
            if value < 1:
                raise ValueError(f"scope.{name}={value} 必须 ≥ 1")
        if from_chapter > to_chapter:
            raise ValueError(
                f"scope 区间非法：from_chapter={from_chapter} > to_chapter={to_chapter}"
            )

    return DistillScope(
        mode=mode,
        ratio=ratio,
        chapters=chapters,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        respect_cutoff=respect_cutoff,
    )


def resolve_project_scope(project, scope: DistillScope | None = None) -> DistillScope:
    """取「本次该用哪个范围」。

    优先级：显式传入 > 项目上存的偏好 > 默认范围。

    ## 为什么范围要存成项目级偏好（与 `narrative_tier` 同理）

    生成链路每次都要核对指纹「这份档案还有效吗」，而指纹里带范围。
    如果生成链路按「默认范围」核对、而用户蒸的是「全量」，
    两者永远对不上 ⇒ **每次生成都会重蒸一遍**，而且重蒸出来的是默认范围那一份
    —— 用户点的「全量」被静默丢掉，界面看上去一切正常。

    ⇒ 蒸馏时把范围记到项目上（`projects.narrative_scope`），
    生成链路按它核对，两边永远一致。
    """
    if scope is not None:
        return scope
    raw = getattr(project, "narrative_scope", None)
    if not raw:
        return DistillScope()
    try:
        return parse_scope(raw)
    except ValueError:
        # 项目上存的范围坏了（老数据 / 手改）⇒ 退回默认范围，
        # 而不是让整条生成链路挂掉。
        return DistillScope()
