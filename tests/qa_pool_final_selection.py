"""QA：候选池「末端择优」的真跑测量台（2026-10-28 加）。

## 这个脚本量什么

`tests/test_candidate_pool_engine.py` 与 `tests/test_pool_form_consistency.py`
用**假 provider / 假尺子**守住了接线与判据（纯机械、可复现）。但下面这几件事
只有真模型给得出来：

1. **落库的正文是不是就是「尺子判过的那份」**（逐字比）；
2. **跑了几条链**、每条链多贵（链 ≈ 10~30 次模型调用）；
3. 形态在链前（先验）与链后（成品）分别是多少 —— **口径差到底落在哪**。

## 怎么跑

```bash
# 需要：一个带**新口径形态档案**的库（档案里要有「纯台词段占比」这一项）
export POOL_API_KEY=sk-...            # 凭据只从环境变量读，不进仓库、不进日志
export POOL_MODE=minimax              # minimax = **生产写手那条**（官方端点 + 生产模型链）
export POOL_MODE=relay                # relay = OpenAI 兼容中转站
export POOL_BASE_URL=https://…/v1     # relay 模式才需要
export POOL_MODEL=qiniu/conatus-free  # relay 模式：端点上**真的能调**的模型
python tests/qa_pool_final_selection.py <库路径> [--pool 3] [--strict 0.8] [--project 1]
```

`--strict 0.8` = 把参考书分布带的**上沿收窄到 80%** —— 用来**逼出重试分支**
（真跑的形态常常第一份就合格 ⇒ 不收窄就看不到「第一份不合格 → 换第二份」）。

输出：`tests/qa_pool_final_selection_run.json`（明细）+ 控制台汇总。

## 为什么脚本里没有「本机绝对路径」

库路径走命令行参数、backend 路径从 `__file__` 推导 —— 这个仓库**不携带任何机器路径**
（见 README「发布卫生」）。凭据同理：只从环境变量读，脚本里没有 key。

## 一条方法论

**「模型没回话」和「判据拒了」在结果里长得一模一样**（与
`qa_lead_on_demand.py` 同一课）。本脚本把「调用失败」与「形态不合格」
**分开统计**：调用失败单独计数并重试，不混进「不合格」那一栏 ——
否则会把通道故障读成判据太严。
"""
from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "backend"))

OUT = os.path.join(REPO, "tests", "qa_pool_final_selection_run.json")

#: 端点上「真的能调」的模型（relay 模式用）。
POOL_MODELS = ("qiniu/conatus-free", "moonshotai/kimi-k3-free")

#: `relay` = OpenAI 兼容中转站；`minimax` = **生产写手那条**。
POOL_MODE = (os.environ.get("POOL_MODE") or "relay").strip().lower()


def _provider():
    """提供方**只**从环境变量拿凭据 —— 不读库、不打印、不落盘。"""
    from app.config import settings
    from app.llm.provider import GenericOpenAIChatProvider, MiniMaxChatProvider

    key = (os.environ.get("POOL_API_KEY") or "").strip()
    if not key:
        raise SystemExit("no api key in env (POOL_API_KEY)")
    if POOL_MODE == "minimax":
        model = (os.environ.get("POOL_MODEL")
                 or (getattr(settings, "minimax_model", "") or "")).strip()
        chain = [model] + [m for m in (settings.minimax_model_list or [])
                           if m != model]
        return MiniMaxChatProvider(
            key, (os.environ.get("POOL_BASE_URL")
                  or settings.minimax_base_url).rstrip("/"), model,
            models=chain,
            max_tokens=int(getattr(settings, "writer_max_tokens", 0) or 0) or None,
            timeout=int(getattr(settings, "writer_timeout", 0) or 0) or 300,
            reasoning_effort=getattr(settings, "minimax_reasoning_effort", None),
        )
    base = (os.environ.get("POOL_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise SystemExit("no base url in env (POOL_BASE_URL)")
    model = (os.environ.get("POOL_MODEL") or POOL_MODELS[0]).strip()
    chain = [model] + [m for m in POOL_MODELS if m != model]
    return GenericOpenAIChatProvider(
        key, base, model, models=chain,
        max_tokens=int(os.environ.get("POOL_MAX_TOKENS", "3000") or 3000),
        timeout=int(os.environ.get("POOL_TIMEOUT", "240") or 240),
    )


def _cjk(text: str) -> int:
    return sum(1 for ch in (text or "") if "一" <= ch <= "鿿")


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit("usage: qa_pool_final_selection.py <db> [--pool N] [--strict X] [--project ID]")
    db_path = argv[0]
    pool_size = 3
    strict = None
    project_id = None
    i = 1
    while i < len(argv):
        flag = argv[i]
        if flag == "--pool":
            pool_size = int(argv[i + 1]); i += 2
        elif flag == "--strict":
            strict = float(argv[i + 1]); i += 2
        elif flag == "--project":
            project_id = int(argv[i + 1]); i += 2
        else:
            raise SystemExit(f"unknown flag: {flag}")

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    from app.db import SessionLocal
    from app.generation import engine as gen_engine
    from app.models import Project

    db = SessionLocal()
    projects = db.query(Project).order_by(Project.id).all()
    if not projects:
        raise SystemExit("库里没有项目")
    picked = None
    for p in projects:
        if project_id and p.id != project_id:
            continue
        if p.style_profile and (p.reference_novel_ids or None):
            picked = p
            break
    if picked is None:
        raise SystemExit("没有「有档案 + 有参考书」的项目（形态打分要靠档案）")
    project = picked

    provider = _provider()
    gen_engine._candidate_pool_size = lambda: pool_size

    # —— 探针：数模型调用、数链 ——
    calls = {"n": 0, "expand": 0}
    _orig_chat = provider.chat

    def counting_chat(system, user, **kwargs):
        calls["n"] += 1
        if "上一版长度不够" in (user or ""):
            calls["expand"] += 1
        return _orig_chat(system, user, **kwargs)

    provider.chat = counting_chat

    chains = []
    _orig_chain = gen_engine._run_text_chain

    def spy_chain(provider_, db_, project_, *, text, **kwargs):
        t0 = time.time()
        final_text, summary, infos = _orig_chain(
            provider_, db_, project_, text=text, **kwargs)
        chains.append({
            "in_chars": _cjk(text),
            "out_chars": _cjk(final_text),
            "out_text": final_text,
            "form_info": (infos or {}).get("form_info") or {},
            "secs": round(time.time() - t0, 1),
        })
        return final_text, summary, infos

    gen_engine._run_text_chain = spy_chain

    if strict:
        _orig_judge = gen_engine._judge_final_text

        def _strict_judge(text, *, profile=None, target_chars=None):
            res = _orig_judge(text, profile=profile, target_chars=target_chars)
            tgt = res.get("target")
            if res.get("checked") and tgt:
                tgt = dict(tgt)
                tgt["p90"] = tgt["p90"] * strict
                value = (res.get("form") or {}).get("value")
                ok = value is not None and tgt["p10"] <= value <= tgt["p90"]
                res["target"] = tgt
                res["ok"] = ok
                if not ok:
                    res["reject"] = (
                        f"形态不在参考书分布带内（收窄带："
                        f"{tgt['p10']:.0%}~{tgt['p90']:.0%}，实测 {value:.0%}）")
            return res

        gen_engine._judge_final_text = _strict_judge

    t0 = time.time()
    out = gen_engine.generate_next_segment(
        db, project, novel_ids=(project.reference_novel_ids or None),
        provider=provider)
    secs = round(time.time() - t0, 1)

    pool = out.get("candidate_pool") or {}
    text = out.get("text") or ""
    committed_is_chain_output = bool(chains) and chains[-1]["out_text"] == text

    result = {
        "mode": POOL_MODE,
        "pool_size": pool_size,
        "strict_band": strict,
        "project_id": project.id,
        "secs": secs,
        "model_calls": calls["n"],
        "expand_calls": calls["expand"],
        "chains": [
            {k: v for k, v in c.items() if k != "out_text"} for c in chains
        ],
        "pool": {k: v for k, v in pool.items() if k != "candidates"},
        "committed_chars": _cjk(text),
        "committed_is_chain_output": committed_is_chain_output,
        "head": text[:120],
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"耗时 {secs}s；模型调用 {calls['n']} 次（闸门扩写 {calls['expand']} 次）；"
          f"跑了 {len(chains)} 条链")
    for i, c in enumerate(chains):
        fi = c["form_info"] or {}
        print(f"  链{i + 1}: 进 {c['in_chars']} 字 → 出 {c['out_chars']} 字，"
              f"{c['secs']}s，形态校正 applied={fi.get('applied')} "
              f"bare_ratio={(fi.get('after') or {}).get('bare_ratio')}")
    print(f"池：试了 {pool.get('n_tried')} 份候选，选第 {pool.get('picked')} 份，"
          f"合格={pool.get('accepted')}，score={pool.get('score')}，"
          f"gated={pool.get('gated')}，mode={pool.get('mode')}")
    for a in pool.get("attempts") or []:
        print(f"  候选#{a.get('index')}: ok={a.get('ok')} "
              f"形态={(a.get('form') or {}).get('value')} score={a.get('score')} "
              f"字数比={a.get('length_ratio')} reject={a.get('reject')}")
    print(f"落库 {result['committed_chars']} 字；"
          f"落库正文 == 选中候选走完链的产物？ {committed_is_chain_output}")
    if pool.get("accepted"):
        verdict = "落库的是**尺子判过的那份**"
    else:
        verdict = ("兜底：没有候选合格 ⇒ 落库**离目标最近**的一份并标注偏离"
                   f"（{pool.get('reason')}）")
    print(f"结论：{verdict}")
    print(f"明细已写：{os.path.relpath(OUT, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
