"""文风档案接入链路的可证伪验证（含真实 DB 往返）。

`verify_stylometry.py` 证的是**统计本身**对不对；这里证的是**接线**对不对：

  A. 体检结论必须随档案一起留痕（判据可能出错，结论要能被人复核）
  B. 指纹缓存：同指纹必须直接命中（这不是优化——档案进的是静态 system 块，
     每次生成都变会击穿提示词缓存），指纹变了必须重算
  C. 提示块：有档案时必须出现且**逐字节确定**；无档案时必须整段不出现
  D. 降级：参考语料挂了 / id 不存在时，必须返回 None 而不是抛异常 ——
     用户点「生成」要的是正文，档案取不到不该让整次生成失败
  E. 落库体积有上限（档案要进 JSON 列 + 每次预检要下发摘要）

所有用例都在**临时项目**上跑，跑完删除，不动用户已有数据。
"""
from __future__ import annotations

import json
import sys
import time

from app.db import SessionLocal, init_db
from app.generation.engine import _style_info, _style_profile
from app.generation.prompts import build_system_prompt
from app.models import Generation, Project, RawNovel
from app.style import build_reference_profile, health_summary, project_profile_safe
from app.style.pipeline import (
    DIGEST_MARK_FIELDS,
    MARK_FIELDS,
    PROFILE_MAX_JSON_BYTES,
    profile_fingerprint,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ← {detail}" if not ok else ""))
    if ok and detail:
        print(f"         {detail}")


def section(t: str) -> None:
    print()
    print("-" * 78)
    print(t)
    print("-" * 78)


def load_corpus(db) -> dict[int, tuple[str, str]]:  # noqa: ANN001
    from sqlalchemy import select

    from app.models import NovelChunk

    out: dict[int, tuple[str, str]] = {}
    for n in db.scalars(select(RawNovel).order_by(RawNovel.id)):
        chunks = db.scalars(
            select(NovelChunk.text).where(NovelChunk.novel_id == n.id).order_by(NovelChunk.idx)
        ).all()
        out[n.id] = (n.title or f"novel:{n.id}", "\n".join(chunks))
    return out


TMP_TITLE = "【验证用·临时】文风档案链路"


def main() -> int:
    init_db()
    db = SessionLocal()
    corpus = load_corpus(db)
    if len(corpus) < 2:
        print("语料不足，无法验证")
        return 1
    dirty_id = max(corpus)  # 《焚天录》 = 被污染那本
    dirty_name = corpus[dirty_id][0]
    print(f"语料：{ {i: t for i, (t, _) in corpus.items()} }")
    print(f"被污染语料：{dirty_name} (id={dirty_id})")

    # 清掉可能残留的临时项目（Generation 无级联，必须手动删）
    old = db.query(Project).filter_by(title=TMP_TITLE).all()
    for o in old:
        db.query(Generation).filter_by(project_id=o.id).delete()
        db.delete(o)
    db.commit()

    proj = Project(title=TMP_TITLE, reference_novel_ids=[dirty_id], generation_length=3000)
    db.add(proj)
    db.commit()
    db.refresh(proj)

    try:
        # ==================================================================
        section("A. 体检结论必须随档案留痕")
        # ==================================================================
        name, text = corpus[dirty_id]
        # 三本一起拟合：这样才能验证「只有被污染那本留下记录，另两本不留」
        prof = build_reference_profile(
            {t: tx for t, tx in corpus.values()}, unit_chars=3000, label="verify"
        )
        h = prof["corpus_health"][name]
        print(f"  chars_in={h['chars_in']} chars_out={h['chars_out']} "
              f"removed={h['removed']} ({h['removed_ratio']*100:.2f}%)")
        for m in h["marks"]:
            print(f"  检出 {m['text']!r} 计数={m['count']} 句尾比={m['final_ratio']} "
                  f"嵌套变体={len(m['nested_variants'])}")
            print(f"    样本: {m['samples'][0][:50]}")
        check("体检结论里有检出串 + 计数 + 句尾比例 + 原文样本",
              bool(h["marks"]) and h["marks"][0]["samples"]
              and h["marks"][0]["count"] > 0 and h["marks"][0]["final_ratio"] > 0.9,
              json.dumps(h["marks"][:1], ensure_ascii=False)[:200])
        # 删除量必须等于「句尾命中次数 × 串长」——这笔账对不上就说明删错了地方
        expect_removed = sum(m["final"] * len(m["text"]) for m in h["marks"])
        check("删除字数 == 句尾命中次数 × 串长（可复核的账）",
              h["removed"] == expect_removed and h["removed"] > 0,
              f"实删 {h['removed']}，应收 {expect_removed}")
        others = {i: v for i, v in prof["corpus_health"].items() if i != name}
        check("未被污染的语料 marks 为空（不制造假阳性记录）",
              all(not v["marks"] and v["removed"] == 0 for v in others.values()),
              str({i: v["marks"] for i, v in others.items()}))
        print(f"  health_summary: {health_summary(prof)}")

        # ==================================================================
        section("B. 指纹缓存：命中要真的省时间，变了要真的重算")
        # ==================================================================
        t0 = time.time()
        p1 = project_profile_safe(db, proj, unit_chars=3000)
        t_first = time.time() - t0
        t0 = time.time()
        p2 = project_profile_safe(db, proj, unit_chars=3000)
        t_hit = time.time() - t0
        print(f"  首次 {t_first:.3f}s  命中 {t_hit:.5f}s")
        check("首次拟合会落库并带指纹",
              p1 and p1.get("fingerprint") == profile_fingerprint([dirty_id], 3000),
              str(p1 and p1.get("fingerprint")))
        check("二次调用命中缓存（< 首次的 1/10，且档案内容一致）",
              t_hit < t_first / 10 and json.dumps(p2, sort_keys=True) == json.dumps(p1, sort_keys=True),
              f"{t_first:.3f}s → {t_hit:.5f}s")
        check("落库确实写回了 projects.style_profile",
              (db.get(Project, proj.id).style_profile or {}).get("fingerprint") is not None)

        t0 = time.time()
        p3 = project_profile_safe(db, proj, unit_chars=1500)
        t_new = time.time() - t0
        check("unit_chars 变了 → 指纹失效、真的重算",
              p3["fingerprint"]["unit_chars"] == 1500 and t_new > t_hit * 10,
              f"{t_new:.3f}s, fingerprint={p3['fingerprint']}")
        check("粒度不对齐会改变档案（所以指纹里必须有 unit_chars）",
              p3["feat"]["句长均值"]["mean"] != p1["feat"]["句长均值"]["mean"],
              f"{p1['feat']['句长均值']['mean']:.3f} vs {p3['feat']['句长均值']['mean']:.3f}")

        # ==================================================================
        section("C. 提示块：有则出现且确定，无则整段消失")
        # ==================================================================
        prof = project_profile_safe(db, proj, unit_chars=3000)
        r = {"character_constraints": [], "unresolved_plotholes": []}
        with_block = build_system_prompt(proj.title, r, _style_info(proj), None, prof)
        again = build_system_prompt(proj.title, r, _style_info(proj), None, prof)
        check("有档案时【文风档案】块出现", "【文风档案" in with_block)
        check("块里有具体数字（不是形容词）",
              "句长" in with_block and any(c.isdigit() for c in with_block))
        check("块里带体检留痕（说明数字是清洗后算的）",
              "已剔除注入串" in with_block and "风沙千里" in with_block)
        check("两次组装 system 逐字节一致（静态块，不击穿缓存）", again == with_block)

        proj.reference_novel_ids = []
        db.commit()
        no_ref = db.get(Project, proj.id)
        no_block = build_system_prompt(
            no_ref.title, r, _style_info(no_ref), None, _style_profile(db, no_ref)
        )
        check("未选参考语料 → 整段不出现（自由生成不该被塞一份别人的文体）",
              "【文风档案" not in no_block)
        check("未选参考语料 → project_profile 返回 None",
              project_profile_safe(db, no_ref, unit_chars=3000) is None)
        proj.reference_novel_ids = [dirty_id]
        db.commit()

        # 未清洗路径要如实自曝
        prof_dirty = build_reference_profile({name: text}, unit_chars=3000, clean=False)
        blk = build_system_prompt(proj.title, r, _style_info(proj), None, prof_dirty)
        check("clean=False 时块里明确标注「未剔除、数字已被污染、仅作弱参考」",
              "未剔除" in blk and "污染" in blk)

        # ==================================================================
        section("D. 降级：档案取不到绝不能拖垮生成")
        # ==================================================================
        proj.reference_novel_ids = [999999]
        db.commit()
        bad = db.get(Project, proj.id)
        t0 = time.time()
        got = project_profile_safe(db, bad, unit_chars=3000)
        check("参考语料 id 不存在 → 返回 None（不抛异常）", got is None,
              "档案只是增强，不该让生成失败")
        print(f"        耗时 {time.time()-t0:.3f}s")

        proj.reference_novel_ids = [dirty_id]
        db.commit()
        ok_again = project_profile_safe(db, db.get(Project, proj.id), unit_chars=3000)
        check("降级之后仍能恢复正常取档（rollback 没把 session 弄脏）",
              ok_again is not None and ok_again.get("fingerprint"))

        # ==================================================================
        section("E. 落库体积")
        # ==================================================================
        raw = json.dumps(ok_again, ensure_ascii=False)
        print(f"  档案 JSON {len(raw.encode('utf-8'))} 字节")
        check("单本档案 JSON 不超过上限（否则预检响应与 JSON 列都吃不下）",
              len(raw.encode("utf-8")) <= PROFILE_MAX_JSON_BYTES,
              f"{len(raw.encode('utf-8'))} 字节，上限 {PROFILE_MAX_JSON_BYTES}")

        # ==================================================================
        section("F. 预检摘要的字段必须齐全（前端按这些字段渲染）")
        # ==================================================================
        # 这条是补的：`marks[].final` 一度没进档案形状，指纹却依然命中，
        # 于是老项目一直拿缺字段的旧档案，预检接口直接 KeyError。
        # 光靠「指纹里加 schema」是防不住的 —— 还得有一道直接盯着字段的闸门。
        from app.routers.generation import _style_profile_digest

        digest = _style_profile_digest(db.get(Project, proj.id), ok_again)
        for f in ("available", "label", "unit_chars", "n_units", "n_train",
                  "n_holdout", "cleaned", "source_chars", "removed_chars",
                  "health_line", "marks", "holdout_p90", "text"):
            check(f"摘要含顶层字段 `{f}`", f in digest and digest[f] is not None,
                  f"{f} = {digest.get(f)!r:.60}")
        miss = [k for k in MARK_FIELDS if not digest["marks"] or k not in digest["marks"][0]]
        check("摘要的每条 marks 含全部约定字段（缺一个前端就渲染不出来）",
              not miss, f"缺：{miss}")
        extra = [k for k in (digest["marks"][0] if digest["marks"] else {})
                 if k not in DIGEST_MARK_FIELDS]
        check("摘要的 marks 不含约定之外的字段（避免把整个档案顺手带下发）",
              not extra, f"多出：{extra}")
        # 新增字段必须同时改 PIPELINE_SCHEMA，否则老档案永远不会重算
        check("指纹里带 schema（档案形状变了才能让老档案失效）",
              "schema" in (ok_again.get("fingerprint") or {}),
              str(ok_again.get("fingerprint")))
        # 老档案（无 schema 键）必须判为新指纹不匹配
        stale = {"fingerprint": {"source_ids": [dirty_id], "unit_chars": 3000,
                                 "version": ok_again["fingerprint"]["version"]}}
        proj.style_profile = stale
        db.commit()
        refreshed = project_profile_safe(db, db.get(Project, proj.id), unit_chars=3000)
        check("缺 schema 的老档案会被判失效并重算（不用写迁移）",
              refreshed is not None and "schema" in (refreshed.get("fingerprint") or {})
              and refreshed.get("corpus_health"), str(refreshed and refreshed.get("fingerprint")))
    finally:
        # 清场：Generation 无级联，必须手动删，否则 id 复用会污染后面新建的项目
        db.rollback()
        p = db.query(Project).filter_by(title=TMP_TITLE).first()
        if p:
            db.query(Generation).filter_by(project_id=p.id).delete()
            db.delete(p)
            db.commit()
        db.close()

    print()
    print("=" * 78)
    print(f"PASS {len(PASSED)}   FAIL {len(FAILED)}")
    for f in FAILED:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
