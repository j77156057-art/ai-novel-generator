"""风格样例的**卫生**：站点垃圾与章标题行不能摆给模型看。

## 为什么

真书实测（990 章）里，盗版站把 SEO 词**插进句子中间**、把广告**整行**塞进章节：

    立即隐相最新章节去别院拿来三根黄瓜
    小宁这才狠狠说道最低调的巨星最新章节：“你说的那笔买卖还作数？”
    请记住本书首发域名：.。三掌门手机版阅读网址：m.

`ingestion/clean.py` 能删掉 87% 的站点垃圾（那些有边界、可整块删的），
剩下 **182 行**全是这种句子中段污染的残句 —— 原来的词**已被替换**，
删掉标记词也拼不回原文。

而风格样例的职责是「把这本书的写法摆给模型看」。摆一行被站点污染过的句子，
模型会把「最新章节」当普通词学。

## 要守住的

1. **不误伤正文**：正文常有的「收藏」「打赏」「更新」「免费」等词不能触发删除；
2. **不修字**：句子中段污染的行只能整行不取用，不能硬修成另一句假话；
3. **章标题行不取用**：它是章节结构标记，不是正文行；
4. **入库语料不动**：这个过滤只作用在「取用侧」（样例），不删用户语料。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.ingestion.clean import is_site_marker_line, strip_site_marker_lines  # noqa: E402
from app.style.exemplar import _split_into_samples  # noqa: E402

NOVEL_LINE = "陆长风握住刀柄，往前半步：“爹，让我进去。”"
SEO_INJECTED = "陆长风估摸一下时分，那白发老魁怎么也应该蹲完茅厕，立即隐相最新章节去别院拿来三根黄瓜"
SITE_AD_LINE = "请记住本书首发域名：.。三掌门手机版阅读网址：m。"
AUTHOR_NOTE = "（更多的更新，已经在微信公众号上传了，大家可以去关注阅读，微信号：fenghuo1985）"


def test_site_marker_line_detection():
    assert is_site_marker_line(SITE_AD_LINE)
    assert is_site_marker_line(AUTHOR_NOTE)
    assert is_site_marker_line(SEO_INJECTED)
    assert not is_site_marker_line(NOVEL_LINE)


def test_normal_prose_words_are_not_markers():
    """正文常有的词不能触发删除 —— 精度比覆盖重要。"""
    for line in (
        "陆长风打赏了一个滚字。",
        "老周说这两千两银子够姑娘们打赏一阵了。",
        "掌柜笑说这批货今日最新，卖得比昨日还快。",
        "读者收藏了这本《北荒刀歌》。",
        "免费是指不收钱，与官府无关。",
    ):
        assert not is_site_marker_line(line), line


def test_strip_keeps_prose_and_drops_junk_lines():
    text = "\n".join([NOVEL_LINE, SEO_INJECTED, SITE_AD_LINE, NOVEL_LINE])
    cleaned, dropped = strip_site_marker_lines(text)
    assert dropped == 2, (cleaned, dropped)
    assert SEO_INJECTED not in cleaned
    assert SITE_AD_LINE not in cleaned
    assert cleaned.count(NOVEL_LINE) == 2, cleaned


def test_strip_is_noop_when_clean():
    text = "\n".join([NOVEL_LINE, "院子里静了下来。"])
    cleaned, dropped = strip_site_marker_lines(text)
    assert cleaned == text
    assert dropped == 0


def test_samples_do_not_contain_junk_or_headings():
    """样例里不能出现站点垃圾行，也不能出现章标题行。"""
    filler = "远处战场中的那个背影，收起了凉刀，从地面上那具尸体上拔出了那根弩箭，像是要拿来当作一根铁矛。"
    text = "\n".join([
        "番外第十章",
        SITE_AD_LINE,
        NOVEL_LINE,
        filler, filler,
        "标长嘿嘿笑着，又是一巴掌拍在姜文生的脑袋上，“还疼不疼了？”",
        filler,
        filler,
    ])
    samples = _split_into_samples(text)
    assert samples, "样例被长度门槛滤光了（测试文本太短）"
    joined = "\n".join(samples)
    assert SITE_AD_LINE not in joined, joined
    assert "番外第十章" not in joined, joined
    assert NOVEL_LINE in joined, joined


def test_inline_chapter_mention_is_kept():
    """正文里出现「第 N 章」字样不能被当成标题行删掉。"""
    line = "他说这一段故事要从第三章讲起，却还没讲完就走了。"
    filler = "远处战场中的那个背影，收起了凉刀，从地面上那具尸体上拔出了那根弩箭，像是要拿来当作一根铁矛。"
    text = "\n".join([line, NOVEL_LINE, filler, filler, filler, filler, filler])
    samples = _split_into_samples(text)
    assert samples, "样例被长度门槛滤光了（测试文本太短）"
    assert any("第三章讲起" in s for s in samples), samples
