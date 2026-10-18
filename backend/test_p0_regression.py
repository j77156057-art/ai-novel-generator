"""P0 回归测试：外部对抗性评审 B 指出的 4 条生产路径缺陷的固化验证。

P0-1  get_reminders 缺 source 条件 → 参考语料角色混入生成约束（已修）
P0-2  plot_bible 存在时静默遮蔽 KG reminders（已修：合并而非二选一）
P0-3  constraints 对真实 provider 无效（已修：追加到 user 末尾，不动静态 system）
P0-4  生成后图谱不进闭环（已修：每段增量 upsert，下次生成可用的 reminders 更新）

全部离线运行，不依赖网络。每次运行重建测试库。
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_p0_test.db")
if os.path.exists(_DB):
    try:
        os.remove(_DB)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.db import Base, SessionLocal  # noqa: E402
from app.generation import build_context, generate_next_segment  # noqa: E402
from app.generation.prompts import build_system_prompt, merged_constraint_items  # noqa: E402
from app.kg.queries import get_reminders  # noqa: E402
from app.models import KgNode, Project  # noqa: E402

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 测试语料：用高置信说话归属信号（X说道）保证规则抽取器必定入图
# ---------------------------------------------------------------------------
SEG1 = (
    "风雪夜，陆见推开木门。\n"
    "陆见说道：“我回来了。”\n"
    "白光说道：“你终于来了。”\n"
    "陆见问道：“师父的死，究竟是谁干的？”\n"
    "白光沉默不语。\n"
)

SEG2 = (
    "第二日。\n"
    "陆见说道：“我要去查清楚。”\n"
    "秦九歌说道：“我陪你一起去。”\n"
    "白光说道：“小心些。”\n"
)


class FakeProvider:
    """按顺序返回固定文本，验证生成→图谱→提醒 的完整闭环。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.idx = 0
        self.seen_constraints = []

    def chat(self, system, user, constraints=None, **kwargs):
        self.seen_constraints.append(constraints)
        t = self.texts[min(self.idx, len(self.texts) - 1)]
        self.idx += 1
        return t

    def summarize(self, text):
        return f"摘要{self.idx}"


def main() -> int:
    Base.metadata.create_all(bind=SessionLocal().bind)
    db = SessionLocal()

    project = Project(
        title="P0回归",
        target_length=3000,
        generation_length=3000,
        genre="仙侠",
        style="起点",
        reference_novel_ids=[],
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id

    print("=" * 72)
    print("P0-1  source 隔离：参考语料角色不得混入生成约束")
    print("=" * 72)
    db.add(
        KgNode(
            project_id=pid,
            type="character",
            props={"name": "林黛玉", "mentions": 99, "speaking": True},
            source="novel:42",
        )
    )
    db.add(
        KgNode(
            project_id=pid,
            type="character",
            props={"name": "陆见", "mentions": 1, "speaking": True},
            source="generated",
        )
    )
    db.commit()

    rem_gen = get_reminders(db, pid)  # 默认 source="generated"
    names_gen = [c["name"] for c in rem_gen["character_constraints"]]
    check("生成约束只含 generated 图角色（陆见）", "陆见" in names_gen, f"得到 {names_gen}")
    check("参考语料角色（林黛玉）不混入生成约束", "林黛玉" not in names_gen, f"得到 {names_gen}")

    rem_ref = get_reminders(db, pid, source="novel:42")
    names_ref = [c["name"] for c in rem_ref["character_constraints"]]
    check("按 source=novel:42 查询能取到参考语料角色", "林黛玉" in names_ref, f"得到 {names_ref}")

    print()
    print("=" * 72)
    print("P0-2  plot_bible 与 KG reminders 的合并语义")
    print("=" * 72)
    # ⚠️ 产品规则在 master 上**被有意反转**（kg-cascade-protocol 阶段 2-3）：
    #   - 旧规则（本脚本原本守的 P0-2）：bible **不得**静默遮蔽 KG reminders，
    #     KG 独有角色一律补充进来。
    #   - 新规则「bible 删除权威」：`bible.characters` **显式列了**名字时，未列入的
    #     KG 角色约束**整条剔除**——用户在 bible 里删掉某角色后，旧抽取事实不得再把
    #     他说成锁定角色（旧段不可改，但未来段只认新设定）。
    #   保守边界仍然保留：bible 无 characters 键或空列表 = 未显式宣告清册 → 不过滤。
    #   所以下面同时断言「显式清册 → 过滤」与「无清册 → 不过滤」两侧，
    #   避免这次反转把边界一起吃掉。
    bible = {"characters": [{"name": "陆见", "role": "主角"}], "foreshadowing": ["师父之死"]}
    reminders = {
        "character_constraints": [{"name": "陆见"}, {"name": "白光"}],
        "unresolved_plotholes": [{"question": "谁干的？"}, {"question": "师父之死"}],
    }
    prompt = build_system_prompt("测试", reminders, {"类型": "仙侠"}, bible)
    check("bible 角色保留（陆见）", "陆见" in prompt)
    check("bible 显式声明清册时，未列入的 KG 角色被剔除（白光）", "白光" not in prompt)
    check("bible 伏笔保留（师父之死）", "师父之死" in prompt)
    check("KG 独有伏笔补充进来（谁干的？）", "谁干的？" in prompt)

    # 合并口径必须是**单一事实来源**，同时供给 system 提示块与 constraints
    #（constraints 会被 provider 追加到 user 末尾做近因提醒）。若两处各写一份，
    # 就会出现「system 里列了、尾部那份自称完整的锁定名单却没列」的自相矛盾，
    # 且漏掉的恰是最高权威层 bible（用户构思）。
    m_chars, m_fores = merged_constraint_items(reminders, bible)
    check(
        "合并口径：bible 显式清册下只保留列入的角色（陆见，白光被剔除）",
        [c["name"] for c in m_chars] == ["陆见"],
        str([c["name"] for c in m_chars]),
    )
    check(
        "合并口径：角色按名去重（陆见只出现一次）",
        [c["name"] for c in m_chars].count("陆见") == 1,
        str([c["name"] for c in m_chars]),
    )
    check(
        "合并口径：伏笔 bible 在前 + KG 补充 + 去重（师父之死不重复）",
        m_fores == ["师父之死", "谁干的？"],
        str(m_fores),
    )
    _no_bible = merged_constraint_items(reminders, None)
    check(
        "无 bible 时合并口径退化为纯 KG 结果",
        _no_bible == (reminders["character_constraints"], ["谁干的？", "师父之死"]),
        str(_no_bible),
    )
    check(
        "空 reminders + 有 bible 时不得报错且以 bible 为准",
        merged_constraint_items({}, bible)[0] == bible["characters"],
        str(merged_constraint_items({}, bible)),
    )

    print()
    print("=" * 72)
    print("P0-3  constraints 对真实 provider 生效（不污染静态 system）")
    print("=" * 72)
    import io  # noqa: E402
    import json  # noqa: E402
    import urllib.request  # noqa: E402

    import app.llm.provider as pm  # noqa: E402

    class _Resp:
        def __init__(self, body):
            self.b = body

        def read(self):
            return self.b

        def __iter__(self):
            return iter(self.b.splitlines(keepends=True))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode()))
        return _Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        provider = pm.OpenRouterChatProvider("k", "http://x", "m")
        provider.chat(
            "SYS",
            "USER正文",
            constraints={"characters": ["陆见", "白光"], "plotholes": ["谁干的？"]},
        )
    finally:
        urllib.request.urlopen = orig

    sent_user = captured["messages"][1]["content"]
    check("约束角色名进入真实 provider 的 user 消息", "陆见" in sent_user and "白光" in sent_user)
    check("约束伏笔进入真实 provider 的 user 消息", "谁干的？" in sent_user)
    check(
        "静态 system 未被污染（缓存不击穿）",
        captured["messages"][0]["content"] == "SYS",
        captured["messages"][0]["content"],
    )

    print()
    print("=" * 72)
    print("P0-4  闭环：段1生成 → 图谱增量更新 → 段2生成前 reminders 可见")
    print("=" * 72)
    fake = FakeProvider([SEG1, SEG2])

    r1 = generate_next_segment(db, project, provider=fake)
    kg1 = r1.get("kg_update") or {}
    print(f"  段1 kg_update = {kg1}")
    check("段1 返回 kg_update 且 merged=True", kg1.get("merged") is True, str(kg1))
    # 注意：P0-1 段已预置 generated 图的「陆见」，故此处 陆见 走 upsert（updated），
    # 白光 为新增。new + updated 合计才代表本段覆盖的角色数。
    check(
        "段1 建图覆盖 ≥ 2 角色（陆见 upsert + 白光 新增）",
        (kg1.get("new_characters") or 0) + (kg1.get("updated_characters") or 0) >= 2,
        str(kg1),
    )
    after1 = [c["name"] for c in get_reminders(db, pid)["character_constraints"]]
    check("段1 后 陆见/白光 均在图中", {"陆见", "白光"} <= set(after1), str(after1))

    # —— 账本闭环（步骤 1）：每段生成后事实入库 + 别名归一 ——
    # 返回形状必须保持扁平兼容：图谱统计在顶层，账本挂在 ledger/aliases 下
    check("kg_update 保留顶层 new_characters（扁平兼容）",
          "new_characters" in kg1, str(sorted(kg1)))
    ld1 = kg1.get("ledger") or {}
    check("段1 事实账本已写入（≥2 条角色事实）",
          (ld1.get("upserted") or 0) >= 2, str(ld1))
    check("kg_update 带回别名归一报告", "aliases" in kg1, str(sorted(kg1)))
    from app.kg.ledger import get_facts as _gf

    lkeys = sorted(f["key"] for f in _gf(db, pid, type_="character"))
    check("账本可读到本段角色", {"陆见", "白光"} <= set(lkeys), str(lkeys))
    lrel = sorted(f["key"] for f in _gf(db, pid, type_="relationship"))
    print(f"  账本关系事实 = {lrel}")
    check("账本记录了关系事实", bool(lrel), str(lrel))
    check(
        "图内无「陆见说/白光说」类粘连伪角色（_SPEAK_RE 非贪婪修复）",
        not any(n in ("陆见说", "白光说", "陆见问") for n in after1),
        str(after1),
    )
    ph1 = get_reminders(db, pid)["unresolved_plotholes"]
    qs = [h.get("question") or "" for h in ph1]
    print(f"  段1 未填坑 = {qs}")
    check(
        "坑 question 不吞下一句（_SEG_RE 吃右引号修复）",
        bool(qs) and all(("\n" not in q and "白光沉默" not in q and not q.endswith("”")) for q in qs),
        str(qs),
    )

    # 关键：段2 生成前，约束必须已含段1 的角色（这就是"闭环"的定义）
    fake.seen_constraints.clear()
    r2 = generate_next_segment(db, project, provider=fake)
    kg2 = r2.get("kg_update") or {}
    print(f"  段2 kg_update = {kg2}")
    cons2 = fake.seen_constraints[0] if fake.seen_constraints else {}
    chars2 = cons2.get("characters") or []
    check("段2 生成时 constraints 已含段1 角色（陆见）", "陆见" in chars2, f"constraints={cons2}")
    check("段2 生成时 constraints 已含段1 角色（白光）", "白光" in chars2, f"constraints={cons2}")

    check("段2 新增角色 ≥ 1（秦九歌）", (kg2.get("new_characters") or 0) >= 1, str(kg2))

    # 增量语义：不清空 → 段1 的 白光 仍在，且被 upsert 累加而非重复新增
    rem_after = get_reminders(db, pid)
    names_after = [c["name"] for c in rem_after["character_constraints"]]
    print(f"  段2后 generated 图角色 = {names_after}")
    check("段2后 白光 仍在（未被清空重建丢失）", "白光" in names_after, str(names_after))
    check("段2后 秦九歌 已入图", "秦九歌" in names_after, str(names_after))
    check(
        "角色无重复条目（upsert 而非重复插入）",
        len(names_after) == len(set(names_after)),
        str(names_after),
    )
    lu = [c for c in rem_after["character_constraints"] if c["name"] == "陆见"]
    check("陆见 mentions 被累加（>=2）", bool(lu) and (lu[0].get("mentions") or 0) >= 2, str(lu))

    # build_context 是真正的"下一次生成入口"，直接验证它
    ctx = build_context(db, project)
    ctx_chars = [c.get("name") for c in ctx["reminders"]["character_constraints"]]
    ctx_cons = ctx["constraints"]["characters"]
    print(f"  build_context reminders = {ctx_chars}")
    print(f"  build_context constraints = {ctx_cons}")
    check("build_context 注入源含段1+段2 全部角色", {"陆见", "白光", "秦九歌"} <= set(ctx_cons))

    # bible 层必须同样进入 constraints，且排在图谱抽取的前面（权威在前）。
    # 这是「system 与 user 尾部口径一致」的集成验证——只测 build_system_prompt 不够，
    # 因为 constraints 走的是 build_context 这条路。
    project.plot_bible = {
        "characters": [{"name": "沈砚", "role": "主角", "motivation": "查明父亲死因"}],
        "foreshadowing": ["铜镜背面的裂纹"],
    }
    db.commit()
    db.refresh(project)
    ctx_b = build_context(db, project)
    b_cons = ctx_b["constraints"]
    print(f"  加 bible 后 constraints = {b_cons}")
    check("bible 角色进入 constraints", "沈砚" in b_cons["characters"], str(b_cons))
    check("bible 伏笔进入 constraints", "铜镜背面的裂纹" in b_cons["plotholes"], str(b_cons))
    check(
        "bible 角色排在图谱抽取角色之前（权威在前）",
        b_cons["characters"][0] == "沈砚",
        str(b_cons["characters"]),
    )
    check(
        "bible 显式清册下，图谱抽取但未列入 bible 的角色被剔除（陆见/白光 不在）",
        not ({"陆见", "白光"} & set(b_cons["characters"])),
        str(b_cons["characters"]),
    )
    check(
        "constraints 与 system 提示同口径（沈砚在两处都出现）",
        "沈砚" in build_system_prompt(
            project.title, ctx_b["reminders"], None, project.plot_bible
        ),
        "system 提示缺 沈砚",
    )
    # 最后一环：provider **实收**的 constraints 里必须有 bible 角色。
    # 只断言 build_context 的返回值不够——真正决定模型看到什么的是 provider 的入参。
    fake.seen_constraints.clear()
    generate_next_segment(db, project, provider=fake)
    recv = fake.seen_constraints[-1] if fake.seen_constraints else {}
    recv_chars = (recv or {}).get("characters") or []
    print(f"  provider 实收 constraints.characters = {recv_chars}")
    check("provider 实收 constraints 含 bible 角色（沈砚）", "沈砚" in recv_chars, str(recv))
    check(
        "provider 实收 constraints 含 bible 伏笔（铜镜背面的裂纹）",
        any("铜镜背面的裂纹" in (h or "") for h in ((recv or {}).get("plotholes") or [])),
        str(recv),
    )
    check(
        "bible 角色在 provider 实收名单里也排最前（权威在前，未被图谱淹没）",
        bool(recv_chars) and recv_chars[0] == "沈砚",
        str(recv_chars),
    )

    # -----------------------------------------------------------------------
    # 附加：实体消解（粘连体 / 切片伪名）——由 P0-4 闭环测试的 fixture 暴露出来的
    # 「宁折 ≠ 宁折枝」家族缺陷，与 P0 同批修复，一并固化。
    # -----------------------------------------------------------------------
    print()
    print("=" * 72)
    print("附加  实体消解：粘连伪角色 + 切片伪名 + 确定性")
    print("=" * 72)

    from types import SimpleNamespace

    from app.kg.extractor import RuleBasedExtractor

    def extract(text: str):
        """铺 2 章：单句用例过不了 Pass 2 的「跨≥2 章」门槛，测不到抽取本身。"""
        chunks = [SimpleNamespace(chapter_no=n, text=text) for n in (1, 2)]
        return set(RuleBasedExtractor().extract(chunks).characters)

    GLUE_CASES = [
        # 说话归属：名 + 副词/动词被粘成 3 字伪角色
        ("陆见说道：“好。”", "陆见", "陆见说"),
        ("贾母又问道：“好。”", "贾母", "贾母又"),
        ("宝玉听说：“好。”", "宝玉", "宝玉听"),
        ("苏晚笑道：“好。”", "苏晚", "苏晚笑"),
        # 显式命名路径
        ("他叫陆见说道：“好。”", "陆见", "陆见说"),
        ("她叫宝玉听说：“好。”", "宝玉", "宝玉听"),
        # 宾语位兜底路径
        ("宝玉见凤姐道：“好。”凤姐也不理会。", "凤姐", "凤姐道"),
        # 反例：真实 3 字名不得被砍短
        ("他叫王守道：“好。”", "王守道", "王守"),
    ]
    for text, want, forbid in GLUE_CASES:
        got = extract(text)
        check(
            f"粘连体不入图：{text[:12]}… → {want}",
            want in got and forbid not in got,
            f"实得 {sorted(got)}",
        )

    # 真实 3 字名（末字非粘连尾字）整体保留，且不产出 2 字切片
    got = extract("秦九歌说道：“好。”")
    check("3 字名完整保留且无切片", got == {"秦九歌"}, f"实得 {sorted(got)}")

    # 切片伪名消除的前提是「长名本身已是候选」（否则无可比较的更长名）。
    # _leading_name 取分句首 2 字，对 3 字名必然切出前缀虚影，这里给出两条真实形态：
    # ① 长名经说话归属入候选 → 虚影必须被剔除
    got = extract("贾宝玉说道：“好。”")
    check("3 字名入候选后，2 字虚影被剔除", got == {"贾宝玉"}, f"实得 {sorted(got)}")
    # ② 源文本硬折行把长名切成两半（公版书扫描档 100% 分片存在这种断行）：
    #    「林黛⏎玉」不得被当成「林黛」的独立出现，必须与另一处的完整「林黛玉」归一
    got = extract("林黛玉说道：“好。”\n林黛\n玉低下头。")
    check(
        "折行切断的名字并回长名（林黛⏎玉 → 林黛玉）",
        "林黛玉" in got and "林黛" not in got,
        f"实得 {sorted(got)}",
    )
    # 反向：短名若另有独立出现，不得被误删
    got = extract("秦九歌问秦九。秦九答了。")
    check(
        "独立出现的短名不被误删（秦九保留）",
        "秦九" in got and "秦九歌" in got,
        f"实得 {sorted(got)}",
    )

    # 切片剔除的判据钉在**文本层**：短名每次出现都被更长候选覆盖即为切片，与那个更长名
    # 自己后续能否过 Pass 2 闸门无关。
    #
    # 曾试过「覆盖者必须能过闸门」——理由是怕「长名被闸门拒，短名又被删，两边都没了」。
    # 真实语料上实测否掉了（写在这里防止后人重复尝试）：
    #   带闸门过滤   1112 角色 / 70 个切片嫌疑
    #   不带闸门过滤 1107 角色 / 65 个切片嫌疑   ← 更优，即当前行为
    # （切片嫌疑用不依赖长名是否存在的独立算子测。）带过滤放回 5 个切片
    #（朱木/甄士/褚遂/贾宝/花阿）却换不回任何长名——因为那些长名（朱木兰/甄士隐/
    # 褚遂良/花阿珍/贾宝玉）本来就被闸门拒。「没替补」的真因是候选发现召不回长名，
    # 属 LLM 抽取阶段的活，不该由这条规则 pass 兜。
    chunks = [
        SimpleNamespace(chapter_no=1, text="朱木兰年十四岁。宝玉拉住朱木兰。"),
        SimpleNamespace(chapter_no=2, text="朱木兰上殿。"),
    ]
    got = set(RuleBasedExtractor().extract(chunks).characters)
    # fix-extractor 3 字名末字校验后「朱木兰」是合法名（兰∈_NAME_CHARS），
    # 切片「朱木」仍应被剔除。断言改为：伪切片不出现，真名允许出现。
    check(
        "切片剔除不看覆盖者强弱（长名弱也照样删切片，不把伪名放回图谱）",
        "朱木" not in got,
        f"实得 {sorted(got)}",
    )

    # 确定性：同一输入两次抽取结果必须完全一致（同长度名的顺序曾由 set 迭代序决定）
    def fingerprint(res):
        return (
            sorted((n, c.mentions, tuple(sorted(c.chapters)))
                   for n, c in res.characters.items()),
            sorted((k[0], k[1], tuple(sorted(v.chapters)))
                   for k, v in res.relationships.items()),
        )

    corpus = [SimpleNamespace(chapter_no=n, text=t) for n, t in enumerate(
        ["宝玉拉住黛玉。", "黛玉说道：“好。”宝玉答道：“嗯。”",
         "宝钗拉住黛玉。", "袭人问宝玉。", "晴雯笑道：“好。”宝玉说道：“嗯。”"],
        start=1,
    )]
    fp1 = fingerprint(RuleBasedExtractor().extract(corpus))
    fp2 = fingerprint(RuleBasedExtractor().extract(corpus))
    check("同输入两次抽取逐项一致（确定性）", fp1 == fp2)

    print()
    print("=" * 72)
    total = len(PASSED) + len(FAILED)
    print(f"结果：{len(PASSED)}/{total} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print(f"  - {f}")
    print("=" * 72)
    db.close()
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
