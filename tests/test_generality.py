"""test_generality.py —— 通用性：不绑定任何特定书/题材

## 为什么要这个测试

项目要求「通用，不专门优化一本小说」。这类缺陷极难发现，因为它在
**当前这本书上完全正常**——只在换书时才暴露。雪中能跑 ≠ 通用。

实际查出两处：
1. SCENE_KEYWORDS 只有都市题材词，武侠/玄幻/科幻/历史 100% 落入「其他」
2. _FAKE_CHARS 硬编码「青鲤帮/青鲤帮/玄真宗/赤澜城」（斗破专名）

## 断言

A. 场景分类不依赖题材：五种题材的典型文本都能被分类（不落「其他」）
B. 场景分类是语料自适应的：同一段文本在不同分布下判定可以不同
C. 场景分布不倾斜：六类占比都在合理区间（不是某一类吃掉 80%）
D. 集合性实体按词形判定：任何书的门派/家族名都能识别
E. 不误伤角色名：2-3 字的真角色名不被判为实体
F. 词表里没有特定书专名（扫描常见网文专名）
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.style.narrative import (
    SCENE_KEYWORDS,
    _FAKE_CHARS,
    _is_collective_entity,
    _is_fake_character,
    classify_scene,
    scene_corpus_stats,
)

# 五种题材，每段 ~300 字，模拟真实章节密度
SAMPLES = {
    "武侠": """北荒王府龙盘虎踞于清凉山，千门万户，极土木之盛。陆长风提刀出府，
刀鞘上缠着旧麻绳，一路走到听潮亭下。他策马出城门，沿着官道往北，山道两旁是枯草。
走了半日，前方渡口有人在等。老周蹲在马厩旁喂马，见他来了，咧嘴一笑。
两人在客栈落座，小二上茶斟酒。""" * 3,
    "玄幻": """沈舟站在赤澜城的拍卖场外，广场上人群密集，满座都是竞价的人。
告示贴在墙上，榜文写着功法名。他沿着街道走到集市，摊贩吆喝声不断。
玄真宗的山门在山腰，台阶一路向上。他独自坐在窗前，四更时分仍未睡。
擂台上有两人交手，刀锋相撞。""" * 3,
    "科幻": """飞船在同步轨道上缓缓转向，舱室的舷窗外是地球的赤道。舰桥上，
指挥官盯着全息投影，引擎的低频震动透过舱壁传进来。他按下通讯键，屏幕亮起。
走廊里人群穿梭，远处传来议论声。他独自回到卧房，夜深了，灯下的数据仍在滚动。
地面上，广场的集结点已经满座。""" * 3,
    "历史": """咸阳宫外，车马辚辚。李斯立于堂下，袖中握着竹简，拱手行礼后落座。
殿内案几上摊着地图，屏风后隐约有人影。群臣俯首，无人敢抬头。
出了宫门，他沿着官道赶往驿站，马车在身后。街市上人群议论纷纷，告示贴满墙。
夜里四更，他独自坐在灯下，翻看竹简。""" * 3,
    "都市": """总裁办公室的门被推开，秘书抱着合同跟进来，把文件摊在会议室桌上。
他把合同逐页签完，落座在客座。窗外广场上人群川流，街市的霓虹亮起。
他独自坐在茶桌前，夜深了还在看那份文件。楼下小路上，车流不断。
大堂里满满一屋子人，议论声不断。""" * 3,
}

#: 常见网文专名——词表里出现任何一个都说明绑定了特定书
BOOK_NOUNS = [
    "陆长风", "陆崇", "北荒", "齐休", "小宁", "老周",
    "沈舟", "青鲤帮", "青鲤帮", "玄真宗", "赤澜城", "卖药翁",
    "韩九", "叶知秋", "石崇", "林川", "牧原", "唐怀",
]


def _fake_text(extra=""):
    """一段中性文本，用于测「不误判」。"""
    return "他走在路上，风很大。" + extra


def test_a_all_genres_classified():
    """A: 五种题材都能被分类，不落「其他」。"""
    chapters = [(i, t) for i, t in enumerate(SAMPLES.values())]
    thr = scene_corpus_stats(chapters)
    failed = []
    for genre, text in SAMPLES.items():
        cls = classify_scene(text, thr)
        if cls == "其他":
            failed.append(genre)
    assert not failed, f"这些题材落入「其他」: {failed}"


def test_b_adaptive_thresholds():
    """B: 阈值来自语料自身，不同分布给出不同阈值。"""
    # 语料 A：全是武侠
    corpus_a = [(i, SAMPLES["武侠"]) for i in range(10)]
    # 语料 B：全是科幻
    corpus_b = [(i, SAMPLES["科幻"]) for i in range(10)]
    ta = scene_corpus_stats(corpus_a)
    tb = scene_corpus_stats(corpus_b)
    assert ta, "阈值不该为空"
    # 两套阈值不该完全相同（说明确实跟着语料走）
    assert any(abs(ta.get(k, 0) - tb.get(k, 0)) > 1e-9 for k in ta), (
        f"阈值与语料无关，说明没做自适应:\nA={ta}\nB={tb}")


def test_c_distribution_not_skewed():
    """C: 分布不倾斜——最大类占比 < 60%。

    旧版「对话/交涉」吃掉 78.9%，那不是「这本书对话多」，
    是判据把通用叙事动词当成了场景标志。
    """
    chapters = [(i, t) for i, t in enumerate(list(SAMPLES.values()) * 4)]
    thr = scene_corpus_stats(chapters)
    from collections import Counter
    dist = Counter(classify_scene(t, thr) for _, t in chapters)
    total = sum(dist.values())
    top = dist.most_common(1)[0]
    assert top[1] / total < 0.6, (
        f"最大类「{top[0]}」占 {top[1]/total*100:.0f}%，分布倾斜: {dict(dist)}")


def test_d_collective_by_shape():
    """D: 任何书的门派/家族名都能按词形识别。"""
    # 3 字以上的门派/家族/场所名（词形规则覆盖）
    collectives = [
        "玄真宗", "少林派", "北荒王府", "天香楼", "青云门", "慕容世家",
        "峨眉派", "华山派", "铁掌帮", "聚贤庄", "大雄寺", "光明殿",
    ]
    missed = [n for n in collectives if not _is_collective_entity(n)]
    assert not missed, f"这些集合性实体没被识别: {missed}"


def test_e_no_false_positive_on_names():
    """E: 真角色名不被误判为实体。"""
    # 含「齐休」（以「山」结尾）——这正是首版误伤的例子
    names = ["陆长风", "齐休", "小宁", "老周", "沈舟", "韩九",
             "叶知秋", "石崇", "林川", "唐怀", "小山", "阿城", "李慕山", "王城"]
    wrong = [n for n in names if _is_collective_entity(n)]
    assert not wrong, f"这些角色名被误判为实体: {wrong}"


def test_f_no_book_specific_nouns():
    """F: 词表里没有特定书专名。"""
    all_words = set(_FAKE_CHARS)
    for kws in SCENE_KEYWORDS.values():
        all_words.update(kws)
    found = [n for n in BOOK_NOUNS if n in all_words]
    assert not found, (
        f"词表里还有特定书专名 {found}——这类词只对一本书有效，"
        f"换书就要手加，应改用词形规则")


def test_g_fake_character_still_works():
    """G: 三层判据合起来仍然正确。"""
    # 虚词
    assert _is_fake_character("相信")
    assert _is_fake_character("冷笑")
    # 集合性实体
    assert _is_fake_character("玄真宗")
    assert _is_fake_character("少林派")
    assert _is_fake_character("明教")  # 2 字门派名靠黑名单
    # 真角色
    assert not _is_fake_character("陆长风")
    assert not _is_fake_character("小宁")
    # 占位符（正则只认「角色X」「具体角色名」这类，不认任意字母串）
    assert _is_fake_character("角色A")
    assert _is_fake_character("具体角色名")
    assert _is_fake_character("")
    assert _is_fake_character("江湖")
    assert _is_fake_character("公子")
    assert _is_fake_character("师父")
    assert not _is_fake_character("陆长风")


def test_h_load_returns_chapters_not_chunks():
    """H: 场景分类必须吃「完整章节」，不能吃 chunk。

    ## 这是一个真实的静默失效

    旧 `_load_novel_chapters` 查 `NovelChunk` 后直接返回，每行是一个
    chunk（约 1000 字），但函数名与所有调用方都当它是「章节」（约 5500 字）。

    后果：`_sample_chapters_v6` 拿 1000 字的 chunk 算场景密度，
    各类型密度都在 0.1-0.4/千字，低于按 chunk 分布算出的 P70 线，
    于是 **100% 的 chunk 被判成「其他」**，场景均衡完全失效
    （实测 5323/5323 全落「其他」）。

    这个 bug 不报错、不丢数据、采样照常出结果——只是「场景均衡」
    静默退化成「按对白密度排序」。极难发现。

    ## 为什么用合成数据测

    真实库测试依赖 sqlalchemy + pydantic，在纯测试环境跑不起来。
    改用合成数据直接验证**不变量**：分类器在 chunk 粒度上会失效，
    在章节粒度上才工作。只要这个不变量成立，加载函数就必须返回章节。
    """
    from app.style.narrative import classify_scene, scene_corpus_stats

    # 合成一本「书」：120 章，主场景轮换六类，但混入杂质词
    # （真实章节从不是纯净的单一场景——这正是为什么密度判据有效）
    MAIN = {
        0: "堂下落座，拱手行礼，案几上茶具齐全。",
        1: "策马出城门，沿官道走了半日，前方渡口。",
        2: "庭院窗棂透光，床榻上躺着人，廊下有人走过。",
        3: "拔刀出鞘，刀锋相撞，剑光闪过，血溅当场。",
        4: "集市人群密集，告示贴满墙，看客围成圈。",
        5: "独自坐在月下，望着窗外，夜深了仍未睡。",
    }
    NOISE = "风从窗外来，他抬头看了一眼，没有说话。"
    chapter_texts = []
    for i in range(120):
        kind = i % 6
        # 主场景词 30 次 + 杂质 40 次 → 主场景密度明显高于杂质
        body = MAIN[kind] * 30 + NOISE * 40
        chapter_texts.append(body)

    # 章节口径
    as_chapters = [(i, t) for i, t in enumerate(chapter_texts)]
    thr_ch = scene_corpus_stats(as_chapters)
    ch_classes = [classify_scene(t, thr_ch) for _, t in as_chapters]
    ch_other_pct = ch_classes.count("其他") / len(ch_classes) * 100

    # chunk 口径（每章切成 5 块，模拟 1000 字 chunk）
    as_chunks = []
    for i, t in enumerate(chapter_texts):
        for k in range(5):
            as_chunks.append((i, t[k * len(t) // 5:(k + 1) * len(t) // 5]))
    thr_ck = scene_corpus_stats(as_chunks)
    ck_classes = [classify_scene(t, thr_ck) for _, t in as_chunks]
    ck_other_pct = ck_classes.count("其他") / len(ck_classes) * 100

    # 不变量：章节口径能分类，chunk 口径明显更差
    assert ch_other_pct < 30, (
        f"章节口径「其他」占 {ch_other_pct:.0f}%——分类器本身有问题")
    assert ck_other_pct > ch_other_pct, (
        f"chunk 口径（{ck_other_pct:.0f}%）没比章节口径（{ch_other_pct:.0f}%）差，"
        f"说明这个不变量不成立，测试需要重新设计")

    # 加载函数必须按 chapter_no 聚合（源码级断言，防回归）
    import inspect

    from app.style.narrative import _load_novel_chapters

    src = inspect.getsource(_load_novel_chapters)
    assert "grouped" in src or "GROUP" in src.upper(), (
        "`_load_novel_chapters` 没做按章聚合——它会返回 chunk 列表，"
        "导致场景分类 100% 落「其他」")
    assert "chapter_no" in src, "应该按 chapter_no 聚合"


def test_i_block_sizing_by_chars():
    """I: 分块按字数不按章数（章长跨书差 3-5 倍）。"""
    from app.style.narrative import (
        MIN_CHAPTERS_PER_CHUNK, TARGET_BLOCK_CHARS, _chapters_per_block,
    )

    # 短章书（1500 字/章）：2 万字需要约 13 章
    short = [(i, "x" * 1500) for i in range(50)]
    # 长章书（8000 字/章）：2 万字只需约 2 章，但要满足最少章数
    long_ = [(i, "x" * 8000) for i in range(50)]

    n_short = _chapters_per_block(short, TARGET_BLOCK_CHARS)
    n_long = _chapters_per_block(long_, TARGET_BLOCK_CHARS)

    assert n_short > n_long, (
        f"短章书该装更多章（{n_short} vs {n_long}），否则块大小失控")
    assert n_long >= MIN_CHAPTERS_PER_CHUNK, "长章书也不该低于最少章数"
    # 短章书装满后字数应接近目标
    assert 1500 * n_short <= TARGET_BLOCK_CHARS * 1.2


def test_j_combat_dimension():
    """J: 「战斗描写」维度存在、可迁移、且只写写法。

    实测背景：加这一维之前，570 条规则里只有 3 条真正讲「战斗怎么写」，
    其余 52 条只是提到兵器或打斗情节。不同作者战斗风格差异极大
    （逐招拆解 / 蒙太奇快切 / 环境破坏 / 旁观者侧写 / 心理博弈），
    现有维度抓不住，必须单独一维。
    """
    from app.style.narrative import (
        _COMBAT_NOISE_PREFIX_RE,
        NARRATIVE_DIMENSIONS,
        _build_combat_prompt,
    )

    assert "战斗描写" in NARRATIVE_DIMENSIONS, "战斗描写应在维度表里"

    system, user = _build_combat_prompt("他拔刀，刀锋相撞。", "语料1 第1章")

    # 1) prompt 必须覆盖五种战斗写法（否则抓不到作者差异）
    for k in ("拆解粒度", "节奏", "视角", "力量如何表现", "语言特征"):
        assert k in system, f"prompt 应问「{k}」"

    # 2) 必须明确禁止写专名与剧情（可迁移性的前提）
    for k in ("招式名", "不要复述"):
        assert k in system, f"prompt 应禁止「{k}」"

    # 3) 输出格式必须与解析分支的 key 一致
    assert '"战斗描写"' in system, "JSON 输出格式的 key 要对上解析分支"

    # 4) 噪声前缀剥掉，但分类标签保留
    assert _COMBAT_NOISE_PREFIX_RE.sub("", "规则描述：打斗以短句推进") == "打斗以短句推进"
    assert _COMBAT_NOISE_PREFIX_RE.sub(
        "", "拆解粒度：不逐招拆写") == "拆解粒度：不逐招拆写"

    # 5) fast 档应跳过它（每 chunk 多一次调用，属于增量能力）
    from app.style.narrative import _FAST_TIER_DROP_DIMENSIONS
    assert "战斗描写" in _FAST_TIER_DROP_DIMENSIONS


TESTS = [
    ("A 五题材都能分类", test_a_all_genres_classified),
    ("B 阈值语料自适应", test_b_adaptive_thresholds),
    ("C 分布不倾斜", test_c_distribution_not_skewed),
    ("D 集合实体按词形", test_d_collective_by_shape),
    ("E 不误伤角色名", test_e_no_false_positive_on_names),
    ("F 无特定书专名", test_f_no_book_specific_nouns),
    ("G 三层判据正确", test_g_fake_character_still_works),
    ("H 加载真章节非chunk", test_h_load_returns_chapters_not_chunks),
    ("I 分块按字数", test_i_block_sizing_by_chars),
    ("J 战斗描写维度", test_j_combat_dimension),
]

if __name__ == "__main__":
    passed = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(TESTS)} pass")
    sys.exit(0 if passed == len(TESTS) else 1)
