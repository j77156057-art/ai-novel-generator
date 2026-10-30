"""角色对话的**角色清单**：把「能扮演的人」从一堆词里挑出来。

## 为什么要有这个模块

2026-10-28 用户报：角色面板里混进大量不是人的东西（水下/水门/别让/清楚/任凭…）。

取证（本地库真实数据，docs/roleplay-character-list-and-answer-quality.md）：

- **项目组**取自 `kg_nodes(source=generated, type=character)` —— 那是**抽取器的原始产物**，
  模型把「水下」「边缘」「颜色」这类**非人称谓**也建成了 character 节点（mentions 高达 47 次）。
- 同一份数据，`kg_entities`（v3 归一化后的实体表）里 **19 个实体个个是人**。

⇒ 修法是**换数据源**，不是加过滤器。

## 参考组的噪声是另一种

声音词典里混进了「作者对读者说话」的样本（"面对读者时语气亲切随和""求月票""请假"），
蒸馏时把**台词里的某个词**当成了角色名（「相当」「简单」「霍然」…）。

实测判据（见设计文档 §1.3）：机械判据无法可靠区分「人名」与「抽错的词」——
`gap0`（名字紧贴说话动词）在 25 条噪声里命中 21 条；作者语汇判据只剔掉 9 条，
但**零误伤**（"肖锵"这类真角色不会被误伤，只要不把「读者」当强信号）。

⇒ 这里只做**确定安全**的那一步：剔掉**强作者信号**的条目（月票/请假/上架/感言/订阅…），
其余一律保留 —— 宁可留噪声，不误伤真人（误伤的代价是用户选不到想扮演的人）。
"""
from __future__ import annotations

#: **强**作者信号：出现在规则文本里 ⇒ 这条讲的是作者跟读者说话，不是书里的人物。
#:
#: 为什么不用「读者」这种泛词：实测《北荒刀歌》里真人「肖锵」的规则文本也含「读者」
#: ⇒ 用泛词会误伤真人。强信号（求月票/请假/上架感言/订阅）实测 **零误伤**。
AUTHOR_TALK_MARKERS: tuple[str, ...] = (
    "月票",
    "求票",
    "求收藏",
    "请假",
    "上架",
    "上架感言",
    "完本感言",
    "更新说明",
    "写在前面",
    "订阅",
    "推荐票",
    "追读",
    "书友",
    "各位读者",
    "作者的话",
    "作品相关",
)


def is_author_talk(rule_text: str) -> bool:
    """这条声音词典规则讲的是「作者对读者说话」吗？

    命中的都是**强**信号 —— 实测在《北荒刀歌》285 条里 0 条命中（零误伤），
    在《长夜司》52 条里剔掉 7 条（全是噪声：能力/危险/相应/白银/成员/利用/曾经）。
    """
    return any(marker in (rule_text or "") for marker in AUTHOR_TALK_MARKERS)


def project_characters(db, project_id: int) -> tuple[list[str], dict[str, list[str]]]:
    """这个项目里的角色（**人**），以及每个角色的别名。

    返回 `(canonical_names, aliases_by_name)`：

    - `canonical_names` —— 规范名（用户点这个）；
    - `aliases_by_name` —— 规范名 → 别名列表（界面上折叠显示，**不单独占一格**）。

    ## 数据源：`kg_entities`，不是 `kg_nodes`

    `kg_nodes` 是抽取器的原始产物（模型把非人称谓也建成了 character 节点）；
    `kg_entities` 是 v3 归一化后的实体表（canonical_name + aliases + 合并），
    实测里面**个个是人**。

    别名不单独占一格的原因：别名往往是「沈砚的父亲」「他爹」「戴斗笠的船夫」这种
    **指代**（不是另一个人），铺开会让清单看起来像多了一倍的角色。
    """
    from app.models import KgEntity

    names: list[str] = []
    aliases: dict[str, list[str]] = {}
    try:
        rows = (
            db.query(KgEntity)
            .filter(KgEntity.project_id == project_id, KgEntity.kind == "character")
            .all()
        )
    except Exception:  # noqa: BLE001 —— 取不到就给空清单，不阻断对话
        return names, aliases
    for row in rows:
        nm = (row.canonical_name or "").strip()
        if not nm:
            continue
        if nm not in names:
            names.append(nm)
        al = []
        for a in (row.aliases or []):
            a = str(a).strip()
            # 「他爹」「那小子」这种纯指代不是别名（指代的是谁已经清楚了）
            if a and a not in al and not a.startswith(("他", "她", "其", "这", "那")):
                al.append(a)
        if al:
            aliases[nm] = al[:6]
    return names, aliases
