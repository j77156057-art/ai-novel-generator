"""章节解析（数字标题 / 卷章混排）与段落级清洗（整段重复 / 跨书污染）的回归测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.ingestion.clean import (  # noqa: E402
    strip_cross_novel_paragraphs,
    strip_duplicate_paragraphs,
)
from app.ingestion.parse_txt import parse_txt  # noqa: E402


def _dash() -> str:
    return "------------\n\n"


# ---------------------------------------------------------------- 章节解析


def test_numeric_titles_with_dunhao():
    """「1、标题」：网文导出的最常见数字标题。"""
    body = "正文段落，若干字。" * 12
    text = _dash() + "".join(
        f"{i}、第{i}个标题\n\n{body}\n\n" for i in range(1, 6)
    )
    chs = parse_txt(content=text)
    assert [c["chapter_no"] for c in chs] == [1, 2, 3, 4, 5]
    assert chs[0]["title"].startswith("1、")


def test_numeric_titles_space_and_paren_and_dot():
    """「1 标题」「(1) 标题」「1. 标题」「01 标题」都要认。"""
    body = "正文段落，若干字。" * 12
    for tpl in ("{i} 第{i}个标题", "({i}) 第{i}个标题", "{i}. 第{i}个标题", "0{i} 第{i}个标题"):
        text = _dash() + "".join(
            f"{tpl.format(i=i)}\n\n{body}\n\n" for i in range(1, 6)
        )
        chs = parse_txt(content=text)
        assert len(chs) == 5, tpl


def test_numeric_titles_do_not_split_body_lines():
    """正文里以数字开头、但**没有空行隔离**的行不能被切成章。"""
    para = (
        "他把行李放下，环视了一圈屋子。\n"
        "1 个人住这么大的房子确实有点空。\n"
        "2 楼的窗户还开着，风一直往里灌。\n"
        "3 天前他刚搬过来，什么都没收拾。\n"
        "4 处墙角堆着没拆的纸箱。\n"
    )
    text = para * 40
    chs = parse_txt(content=text)
    assert len(chs) == 1


def test_volume_then_chapter_not_counted_as_two_chapters():
    """卷章混排：卷只作边界，不单独成章；章号连续。"""
    body = "正文段落，若干字。" * 12
    text = _dash()
    for v in range(1, 3):
        text += f"第{v}卷 卷名{v}\n\n"
        for c in range(1, 4):
            text += f"第{c}章 第{v}卷第{c}章标题\n\n{body}\n\n"
    chs = parse_txt(content=text)
    assert len(chs) == 6
    assert [c["chapter_no"] for c in chs] == [1, 2, 3, 4, 5, 6]
    assert all("卷" not in c["title"][:2] for c in chs)


def test_volume_prefixed_chapter_title():
    """「第二集 众生相 第一章 标题」这种合并标题要认成章。"""
    body = "正文段落，若干字。" * 12
    text = _dash()
    for v in range(1, 3):
        text += f"第{v}集 集名{v}\n\n"
        for c in range(1, 4):
            text += f"第{v}集 集名{v} 第{c}章 标题{c}\n\n{body}\n\n"
    chs = parse_txt(content=text)
    assert len(chs) == 6


def test_volume_only_book_uses_volumes_as_chapters():
    """只有「卷」没有任何「章」的书，按卷切。"""
    body = "正文段落，若干字。" * 12
    text = _dash() + "".join(
        f"第{i}卷 卷名{i}\n\n{body}\n\n" for i in range(1, 5)
    )
    chs = parse_txt(content=text)
    assert len(chs) == 4


def test_numeric_reset_not_accepted_without_volume_markers():
    """文末「1、微信—…2、…」这类推广列表不能被当成按卷重置。"""
    body = "正文段落，若干字。" * 12
    text = _dash()
    for i in range(1, 21):
        text += f"正文 正文_{i:03d}标题{i}\n\n{body}\n\n"
    text += (
        "1、微信—点右上角的加号—添加朋友—公众号—输入：某某\n\n"
        "2、微信—通讯录—公众号—搜索：某某\n\n"
    )
    chs = parse_txt(content=text)
    assert len(chs) == 20


# ---------------------------------------------------------------- 错误页拦截


def test_html_error_page_dropped_and_reported():
    """抓取失败的 HTML 500 页面不能当正文：丢弃、计入 parse_failed、章号连续。"""
    body = "正文段落，若干字。" * 12
    err = (
        "<html><head><title></title><style></style> </head><body>"
        "<h1>http status 500 - string index out of range: -10</h1>"
        '<hr size="1" noshade="noshade">\n    type exception report\n'
        "</body></html>"
    )
    text = _dash()
    for i in range(1, 6):
        text += f"第{i}章 标题{i}\n\n{err if i == 3 else body}\n\n"

    report: dict = {}
    chs = parse_txt(content=text, report=report)
    assert len(chs) == 4
    assert [c["chapter_no"] for c in chs] == [1, 2, 3, 4]
    assert report["parse_failed_count"] == 1
    assert report["parse_failed"][0]["reason"] == "html_error_page"
    assert all("http status" not in c["body"] for c in chs)


def test_normal_body_with_angle_brackets_not_mistaken_for_error_page():
    """正文里的 `<`、代码段、英文 HTTP 字样不能被误判成错误页。"""
    body = (
        "他在纸上写下：if a < b and b <= c，这段代码就能跑通。\n"
        "调试时控制台弹出 <div> 和 </div> 的标签示例，他看了一眼就关掉了。\n"
        "同事说服务器白天返回过 HTTP status 500，那只是一次事故复盘。\n"
        "<html> 这个标签在教程第一页就讲过，没什么稀奇。\n"
    ) * 10
    text = _dash() + "".join(f"第{i}章 标题{i}\n\n{body}\n\n" for i in range(1, 6))

    report: dict = {}
    chs = parse_txt(content=text, report=report)
    assert len(chs) == 5
    assert report["parse_failed_count"] == 0
    assert "<div>" in chs[0]["body"]


# ---------------------------------------------------------------- 段落级清洗


def test_duplicate_paragraphs_keep_first_copy():
    p = "这是一段足够长的正文段落，用来测试整段重复的识别与保留策略是否生效。"
    uniques = [
        f"第{i}段唯一的正文内容，长度也足够长，用来把重复占比压到局部水平不会超过阈值。"
        for i in range(10)
    ]
    text = "\n\n".join(uniques + [p, p, p])
    cleaned, report = strip_duplicate_paragraphs(text)
    assert cleaned.count(p) == 1
    assert report["form"] == "partial"
    assert report["removed_paragraphs"] == 2
    assert report["removed_chars"] == len(p.replace("，", "").replace("。", "")) * 2
    assert report["samples"][0]["count"] == 3


def test_near_full_double_detected():
    p = "这是一段足够长的正文段落，用来测试近乎整本双份粘贴的判定形态是否正确。"
    q = "这是另一段足够长的正文段落，同样用于验证双份形态的统计口径是否准确。"
    text = "\n\n".join([p, q, p, q])
    cleaned, report = strip_duplicate_paragraphs(text)
    assert report["form"] == "near_full_double"
    assert report["duplicate_ratio"] >= 0.35
    assert cleaned.count(p) == 1 and cleaned.count(q) == 1


def test_cross_novel_paragraph_removed_with_all_copies():
    异书 = (
        "名师大陆之上，灵魂契约燃烧，天道之册浮现，所有弟子都在等待那一道光。"
        "长老抬手，神界之门缓缓开启，众人齐声欢呼，声浪传遍整座山门久久不散。"
    )
    正文本 = "许七安走进衙门，把铜锣往桌上一放，抬头看了看天色，觉得今天大概不会太平。"
    text = "\n\n".join([正文本, 异书, 正文本, 异书, 正文本, 异书])
    cleaned, report = strip_cross_novel_paragraphs(text)
    assert report["detected_groups"] == 1
    assert report["removed_paragraphs"] == 3  # 全部副本一起删
    assert "名师大陆" not in cleaned
    assert "许七安" in cleaned
    assert report["samples"][0]["tokens"]


def test_repeated_in_book_paragraph_not_treated_as_cross_novel():
    """书内自己的段落被重复时不能判成异书：它的用词在本书别处也会出现。

    真实样本是《我不是戏神》里重复 12 次的「剧目的综合观众期待值」说明 ——
    40 个候选词里 20 个组外 0 次（占比 0.50），若只看个数就会误删。
    这里用「同一套设定说法的多个变体」复现该形态。
    """
    p = "使用后，你可以从本次剧目的所有出场角色中，指定某个角色，随机抽取对方的能力。"
    变体 = [
        f"使用后，你可以从第{i}场剧目的出场角色中，指定某个角色，随机抽取对方的一项能力。"
        for i in range(6)
    ]
    text = "\n\n".join([变体[0], p, 变体[1], p, 变体[2], p] + 变体[3:])
    cleaned, report = strip_cross_novel_paragraphs(text)
    assert report["detected_groups"] == 0
    assert cleaned.count(p) == 3


def test_whole_book_doubled_is_not_treated_as_cross_novel():
    """整本近乎双份时，跨书判定必须让位给「保留首次」的去重（否则会整本删光）。"""
    paras = [
        f"第{i}段正文写得足够长，用来制造一个整本被复制粘贴三遍的文件形态，"
        f"每一段的字数都超过四十个汉字，好让它参与跨书污染的判定。"
        for i in range(12)
    ]
    text = "\n\n".join(paras * 3)
    cleaned, report = strip_cross_novel_paragraphs(text)
    assert report["detected_groups"] == 0
    assert report.get("skipped") == "near_full_duplicate"
    assert cleaned.count(paras[0]) == 3  # 这一步不动文本，交给去重处理
