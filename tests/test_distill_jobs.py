"""文风蒸馏后台任务（DistillRun + 三个 /distill 端点 + 进度回调）。

## 为什么要单独建档这组测试

蒸馏是**分钟级**长任务（833 章语料跑完整档实测 42 分钟），用户唯一的感知渠道
就是这套「任务表 + 轮询」。它一旦静默失效（点了没跑 / 失败了不说 / 进度永远
停在 running），排查成本极高——而且全都表现为「界面没反应」。

这里钉住四条硬保证：

1. 提交端点**立即返回** job_id，后台跑完后任务落到 success 并回填摘要；
2. 任何异常都要落到 `failed` + `error` + `finished_at`，**绝不**停在 running；
3. `progress` 回调只负责上报 —— 传与不传，蒸馏产出必须逐字节一致；
4. 静态子段路由不被 `/{project_id}` 遮蔽（`narrative-tiers` 踩过一次）。

## 假 provider

与 `tests/test_style_tier.py` 同款：绝不真调 LLM。`extract_narrative_profile`
是在函数体内部 `from app.llm.provider import get_llm_provider`，所以 monkey-
patch `app.llm.provider.get_llm_provider` 即可生效。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import DistillRun, NovelChunk, Project, RawNovel  # noqa: E402
from app.style import narrative as narrative_mod  # noqa: E402
from app.style.jobs import (  # noqa: E402
    STATUS_FAILED,
    STATUS_SUCCESS,
    run_distill_job,
)
from app.style.narrative import TIER_FAST, extract_narrative_profile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures / 工具
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_db():
    """每个用例重建库：**distill_runs 是被碾压风险最高的表**（它是新表）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


class StubProvider:
    """确定性假 provider：返回一条**证据能通过**的「人物描写手法」规则。

    证据句子必须真的出现在语料里（否则被 `_validate_evidence` 拦掉变成 rejected），
    这样才能断言「成功路径真的产出了规则」，而不是只断言「没崩」。
    """

    #: 与 `_make_corpus` 里的正文严格对齐
    EVIDENCE = "医院里她抬起头"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        # 证据串必须**原样**落在语料里，且规则整体不是原文的某个句子
        # （否则会被 `_is_verbatim_copy` 当成「逐字复制」拒掉）。
        return '{"人物描写手法": ["出场先写环境再写人（证据：' + self.EVIDENCE + '）"]}'


class BoomProvider:
    """每次调用都炸的 provider：用来验证失败不是静默的。"""

    def chat(self, system: str, user: str) -> str:
        raise RuntimeError("模型服务不可用（测试注入）")


@pytest.fixture
def stub_llm(monkeypatch):
    """把 `get_llm_provider` 换成 StubProvider，并返回同一个实例以便计数。"""
    stub = StubProvider()
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: stub)
    return stub


def _make_corpus(db, n_chapters: int = 30) -> int:
    """造一本 n_chapters 章的语料，返回 novel_id。"""
    novel = RawNovel(title=f"蒸馏任务测试语料{n_chapters}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text=(
                    "医院里她抬起头，看着走廊尽头的光。"
                    + "他说：走吧。" * (i % 3 + 1)
                ),
            )
        )
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, tier: str | None = None) -> Project:
    proj = Project(
        title="蒸馏任务测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        reference_novel_ids=[novel_id],
        narrative_tier=tier,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


# ====================================================================
# 1. 进度回调：只上报，不影响产出
# ====================================================================


def test_progress_callback_reports_stage_and_counters(
    db_session, stub_llm
):
    """进度回调要给出「阶段文字 + 已完成/总数」，且是可用的整数。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    calls: list[tuple[str, int, int]] = []
    extract_narrative_profile(
        db_session,
        proj,
        [nid],
        provider=stub_llm,
        tier=TIER_FAST,
        progress=lambda s, c, t: calls.append((s, c, t)),
    )

    assert calls, "没收到任何进度上报——进度条会永远是空白"
    for stage, current, total in calls:
        assert isinstance(stage, str) and stage.strip()
        assert isinstance(current, int) and current >= 0
        assert isinstance(total, int) and total >= 0
        # 单位必须是「块」：总数不能小于当前完成数，否则百分比会越界 100%。
        assert total >= current, f"进度倒挂 stage={stage} {current}/{total}"

    # 结束时必须已经探明总数：0 会让前端永远显示不确定态
    last_stage, last_cur, last_total = calls[-1]
    assert last_total > 0, "结束时总数仍为 0 —— 进度条会卡在不确定态"
    assert last_cur == last_total, "结束时应对齐到 100%"
    assert "汇总" in last_stage


def test_progress_is_truly_optional_no_output_change(db_session, stub_llm):
    """**不传** progress 时行为必须与加进度之前逐字节一致。

    这是本组的命门断言：蒸馏产出被进度回调污染的话，生成路径（它从不传
    progress）会拿到与后台任务不同的档案，且无从察觉。
    """
    calls: list[tuple[str, int, int]] = []

    def _run(progress):
        return extract_narrative_profile(
            db_session, proj, [nid], provider=stub_llm, tier=TIER_FAST,
            progress=progress,
        )

    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    with_progress = _run(lambda s, c, t: calls.append((s, c, t)))
    without_progress = _run(None)

    assert calls, "传了回调却没上报"
    # 剥掉 fingerprint（两个库对象的时间戳/ id 不同），比对**蒸馏产出本身**
    for p in (with_progress, without_progress):
        p.pop("fingerprint", None)
    assert with_progress == without_progress


# ====================================================================
# 2. 后台执行体：成功要回填摘要，失败绝不停在 running
# ====================================================================


def test_run_distill_job_success_fills_summary(db_session, stub_llm):
    """成功路径：status=success + 摘要字段 + 进度对齐 100% + 落 finishing 时间戳。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    from app.style.jobs import create_distill_run

    run = create_distill_run(db_session, proj.id, TIER_FAST)
    run_distill_job(run.id, proj.id, TIER_FAST, False)

    db_session.expire_all()
    done = db_session.get(DistillRun, run.id)
    assert done.status == STATUS_SUCCESS
    assert done.stage == "蒸馏完成"
    assert done.error is None
    assert done.started_at is not None and done.finished_at is not None
    # 摘要字段必须是回填过的整数，不能留 null 让前端显示「未知」
    assert isinstance(done.n_chapters, int)
    assert isinstance(done.rule_count, int)
    assert isinstance(done.rejected, int)
    # 假 provider 的证据是语料里真实存在的句子 → 至少提炼出一条规则
    assert done.rule_count > 0, "成功却没产出任何规则，摘要字段将永远为 0"
    assert done.progress_total > 0 and done.progress_current == done.progress_total


def test_run_distill_job_failure_is_recorded_not_silent(db_session, monkeypatch):
    """失败路径：**必须**写 failed + error + finished_at。

    停在 running 的表现是「进度条一直转但不报错」，比失败更难排查。
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("蒸馏炸了（测试注入）")

    monkeypatch.setattr(narrative_mod, "project_narrative", _boom)

    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    from app.style.jobs import create_distill_run

    run = create_distill_run(db_session, proj.id, TIER_FAST)
    run_distill_job(run.id, proj.id, TIER_FAST, False)

    db_session.expire_all()
    done = db_session.get(DistillRun, run.id)
    assert done.status == STATUS_FAILED
    assert done.stage == "蒸馏失败"
    assert "蒸馏炸了（测试注入）" in (done.error or ""), "失败原因没落到 error 列"
    # 失败也要有终点时间戳：没有它，前端算不出「跑了多久」，也区分不了卡住/失败
    assert done.finished_at is not None


def test_run_distill_job_rejects_project_without_corpus(db_session):
    """没选参考语料 → 明确失败，而不是「成功但什么都没干」的自由生成。"""
    proj = Project(
        title="无语料项目", target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)

    from app.style.jobs import create_distill_run

    run = create_distill_run(db_session, proj.id, TIER_FAST)
    run_distill_job(run.id, proj.id, TIER_FAST, False)

    db_session.expire_all()
    done = db_session.get(DistillRun, run.id)
    assert done.status == STATUS_FAILED
    assert "未选参考语料" in (done.error or "")


# ====================================================================
# 3. 三条端点
# ====================================================================


def test_static_sub_paths_are_not_shadowed(db_session):
    """路由顺序护栏：`narrative-tiers` 与 `distill/*` 都活着。

    `/{project_id}` 声明在 `/{project_id}/distill` **之后**才是安全的。这里的
    断言方式是：静态子段返回 200/404 的结构化结果，而不是被 `project_id: int`
    拒成 422。
    """
    client = TestClient(fastapi_app)
    r = client.get("/api/projects/narrative-tiers")
    assert r.status_code == 200, f"narrative-tiers 被遮蔽了：{r.status_code}"
    assert r.json()["tiers"]

    # 不存在的项目：404 说明路由走到了 handler，而不是被 path 参数类型拒掉
    r2 = client.get("/api/projects/999999/distill/jobs")
    assert r2.status_code == 404


def test_start_distill_returns_job_id_and_runs_to_success(db_session, stub_llm):
    """端到端：POST 立即返回 job_id → BackgroundTasks 跑完 → 轮询拿到 success。

    TestClient 会在响应发出后同步执行 BackgroundTasks，所以这里断言得到的是
    终态；生产环境里则是真的异步跑（几十分钟）。
    """
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)

    client = TestClient(fastapi_app)
    r = client.post(
        f"/api/projects/{proj.id}/distill", json={"tier": TIER_FAST}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] > 0
    assert body["tier"] == TIER_FAST
    assert body["status"] in ("pending", "running"), "提交时应立即返回未终态"
    assert body["reused"] is False

    job = client.get(f"/api/projects/{proj.id}/distill/jobs/{body['job_id']}").json()
    assert job["status"] == STATUS_SUCCESS
    assert job["stage"] == "蒸馏完成"
    assert job["progress_total"] > 0
    assert job["progress_current"] == job["progress_total"]
    assert job["finished_at"] is not None
    # 出参不能带 style_narrative 全文：1.5s 一次轮询传几万字符是纯浪费
    assert "style_narrative" not in job

    # 项目上的档位偏好被顺带落库（后续不带 tier 的调用也按它来）
    db_session.expire_all()
    assert db_session.get(Project, proj.id).narrative_tier == TIER_FAST


def test_unknown_tier_is_rejected_before_any_llm_call(db_session, stub_llm):
    """非法档位在**提交时**就 400，不留成一个跑 40 分钟才失败的后台任务。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)

    client = TestClient(fastapi_app)
    r = client.post(f"/api/projects/{proj.id}/distill", json={"tier": "turbo"})
    assert r.status_code == 400
    assert "未知蒸馏档位" in r.json()["detail"]
    assert stub_llm.calls == 0, "非法档位居然还是调了 LLM"


def test_unknown_body_field_is_rejected(db_session, stub_llm):
    """拼错字段必须 422，不能静默按默认档位跑一整套蒸馏。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)

    r = TestClient(fastapi_app).post(
        f"/api/projects/{proj.id}/distill", json={"tiers": TIER_FAST}
    )
    assert r.status_code == 422


def test_start_distill_reuses_active_run_of_same_tier(db_session, stub_llm):
    """并发防重：已在跑的任务被复用，不重复烧 LLM（同档位场景）。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    from app.style.jobs import create_distill_run

    active = create_distill_run(db_session, proj.id, TIER_FAST)

    client = TestClient(fastapi_app)
    body = client.post(
        f"/api/projects/{proj.id}/distill", json={"tier": TIER_FAST}
    ).json()
    assert body["job_id"] == active.id
    assert body["reused"] is True
    assert stub_llm.calls == 0, "复用路径不该触发新的蒸馏"


def test_start_distill_reuses_active_run_regardless_of_tier(db_session, stub_llm):
    """并发防重**不分档位**（P2-1 修复）：已有未终态任务时换档位也复用。

    旧契约是「同档位才复用、换档位新起」——那会让两个后台线程共写同一
    project.style_narrative：后完成的覆盖先完成的（可能旧档盖新档），
    且争抢 SQLite 写锁。想换档位重算，等当前任务终态后再提交。
    """
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    from app.style.jobs import create_distill_run

    active = create_distill_run(db_session, proj.id, TIER_FAST)

    client = TestClient(fastapi_app)
    body = client.post(
        f"/api/projects/{proj.id}/distill", json={"tier": "full"}
    ).json()
    assert body["job_id"] == active.id, "换档位也该复用未终态任务，不能双线程齐跑"
    assert body["reused"] is True
    assert body["tier"] == TIER_FAST, "复用的是原任务的档位，回参必须如实"
    assert stub_llm.calls == 0, "复用路径不该触发新的蒸馏"


def test_list_jobs_is_descending_and_project_scoped(db_session):
    """列表：按 project 隔离 + id 降序（前端取第一条即「最近一次」）。"""
    p1 = Project(title="甲", target_length=5000, mode="quick", status="created")
    p2 = Project(title="乙", target_length=5000, mode="quick", status="created")
    db_session.add_all([p1, p2])
    db_session.commit()

    from app.style.jobs import create_distill_run

    r1 = create_distill_run(db_session, p1.id, TIER_FAST)
    r2 = create_distill_run(db_session, p1.id, TIER_FAST)
    other = create_distill_run(db_session, p2.id, TIER_FAST)

    client = TestClient(fastapi_app)
    items = client.get(f"/api/projects/{p1.id}/distill/jobs").json()["items"]
    assert [i["id"] for i in items] == [r2.id, r1.id], "应为 id 降序且不含别的项目"
    assert other.id not in [i["id"] for i in items]

    # job 与 project 不匹配 → 404，不能凭 id 猜到别的项目在跑什么
    assert (
        client.get(f"/api/projects/{p1.id}/distill/jobs/{other.id}").status_code
        == 404
    )


def test_api_shape_matches_frontend_distill_job_contract(db_session):
    """自己在 total=0 时不许显示 0% —— 前端靠 total===0 判不确定态。

    这里钉住「pending 行的 total 必须是 0」：如果以后改成 1，前端的判断会
    从「不确定态」悄悄变成「已完成 0/1」，语义就错了。
    """
    proj = Project(title="形状测试", target_length=5000, mode="quick", status="created")
    db_session.add(proj)
    db_session.commit()

    from app.style.jobs import create_distill_run, distill_run_to_dict

    run = create_distill_run(db_session, proj.id, TIER_FAST)
    shape = distill_run_to_dict(run)

    expected_keys = {
        "id", "project_id", "tier", "status", "stage", "progress_current",
        "progress_total", "n_chapters", "rule_count", "rejected", "error",
        "created_at", "started_at", "finished_at",
    }
    assert set(shape) == expected_keys
    assert shape["progress_total"] == 0, "pending 行总数只能是 0（= 未探明）"
    assert shape["progress_current"] == 0
    assert shape["finished_at"] is None
    # 摘要三字段在失败/未跑时必须为 null，不许用 0 蒙混
    assert shape["n_chapters"] is None
    assert shape["rule_count"] is None
    assert shape["rejected"] is None


# ====================================================================
# 4. 蒸馏 provider：配了 distiller 就用它，没配则回落（老行为零变化）
# ====================================================================


class MarkerProvider:
    """带标记的假 provider——用来断言「到底调的是哪一个」。

    不真打 LLM，只记 `MARK` 便于断言。`chat` 返回一条**合法且证据能过**的
    规则（不是空 JSON）：空 JSON 不含任何预期维度键，会被失败块护栏当成
    「不可用输出」计失败——那会让本组「选provider」的测试被无关护栏绊倒。
    """

    MARK = "marker"

    def __init__(self, mark: str) -> None:
        self.mark = mark
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return '{"人物描写手法": ["出场先写环境再写人（证据：医院里她抬起头）"]}'


def test_distill_uses_distiller_provider_when_configured(monkeypatch):
    """配了 distiller → 蒸馏实际调用的是 distiller 的 provider。

    这是「蒸馏模型选择」从摆设变真生效的命门断言：以前界面能选但链路不读，
    用户配了也白配。
    """
    import app.llm as llm_mod
    from app.style.narrative import resolve_distill_provider

    distiller = MarkerProvider("distiller")
    writer = MarkerProvider("writer")
    monkeypatch.setattr(llm_mod, "get_distiller_provider", lambda: distiller)
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: writer)

    chosen = resolve_distill_provider()
    assert chosen is distiller, "配了 distiller 却没用它 —— 蒸馏模型选择是摆设"

    # 端到端走一遍：真的调到了 distiller，且一次都没碰 Writer
    db = SessionLocal()
    try:
        nid = _make_corpus(db, 20)
        proj = _make_project(db, nid, tier=TIER_FAST)
        extract_narrative_profile(db, proj, [nid], tier=TIER_FAST)
    finally:
        db.close()
    assert distiller.calls > 0
    assert writer.calls == 0, "走了 distiller 就不该再碰 Writer"


def test_distill_falls_back_to_writer_when_distiller_unconfigured(monkeypatch):
    """没配 distiller（provider 降级为 LocalFallback）→ 回落 Writer，老行为不变。

    少了这条回落，从没配过 distiller 的老用户会从「自己配的模型」静默退化成
    本地模板模型 —— 不报错、只是产出变差，是最难发现的那类回归。
    """
    import app.llm as llm_mod
    from app.llm.provider import LocalFallbackLLMProvider
    from app.style.narrative import resolve_distill_provider

    writer = MarkerProvider("writer")
    # 未配置时的真实返回值：build_role_provider 在 provider 空 / 无 key 时给兜底
    monkeypatch.setattr(llm_mod, "get_distiller_provider", LocalFallbackLLMProvider)
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: writer)

    assert resolve_distill_provider() is writer


def test_distill_falls_back_when_distiller_resolution_raises(monkeypatch):
    """配置损坏导致解析抛错 → 也不能让蒸馏链路整个炸掉，同样回落。"""
    import app.llm as llm_mod
    from app.style.narrative import resolve_distill_provider

    writer = MarkerProvider("writer")

    def _boom():
        raise RuntimeError("distiller 配置损坏（测试注入）")

    monkeypatch.setattr(llm_mod, "get_distiller_provider", _boom)
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: writer)

    assert resolve_distill_provider() is writer


# ====================================================================
# 5. 启动收割：卡住的后台任务必须被收掉（否则进度条永远转）
# ====================================================================


def test_reap_stale_runs_marks_running_and_pending_as_failed(db_session):
    """running / pending 都要被收割成 failed + 有原因 + 有终点时间戳。"""
    from app.style.jobs import REAPED_ERROR, create_distill_run, reap_stale_runs

    p = Project(title="收割测试", target_length=5000, mode="quick", status="created")
    db_session.add(p)
    db_session.commit()

    pending = create_distill_run(db_session, p.id, TIER_FAST)
    running = create_distill_run(db_session, p.id, TIER_FAST)
    running.status = "running"
    db_session.commit()

    reaped = reap_stale_runs(db_session)

    assert reaped == 2, f"两条未终态任务都该被收割，实际 {reaped}"
    db_session.expire_all()
    for rid in (pending.id, running.id):
        row = db_session.get(DistillRun, rid)
        assert row.status == STATUS_FAILED
        assert row.finished_at is not None, "收割后必须能算出「跑了多久」"
        assert row.started_at is not None, "pending 行没 started_at 也要补上"
        assert row.error == REAPED_ERROR


def test_reap_stale_runs_never_touches_terminal_rows(db_session):
    """**已终态的行绝不能被改写** —— 那是用户上一次蒸馏的结论，覆盖就找不回来。"""
    from app.style.jobs import create_distill_run, reap_stale_runs

    p = Project(title="收割测试2", target_length=5000, mode="quick", status="created")
    db_session.add(p)
    db_session.commit()

    ok = create_distill_run(db_session, p.id, TIER_FAST)
    ok.status = STATUS_SUCCESS
    ok.stage = "蒸馏完成"
    ok.rule_count = 12
    bad = create_distill_run(db_session, p.id, TIER_FAST)
    bad.status = STATUS_FAILED
    bad.error = "真失败（不是重启）"
    db_session.commit()

    assert reap_stale_runs(db_session) == 0, "没有未终态任务时不该收割任何行"

    db_session.expire_all()
    assert db_session.get(DistillRun, ok.id).status == STATUS_SUCCESS
    assert db_session.get(DistillRun, ok.id).rule_count == 12
    assert db_session.get(DistillRun, bad.id).status == STATUS_FAILED
    assert db_session.get(DistillRun, bad.id).error == "真失败（不是重启）", (
        "真实失败原因被重启收割覆盖了 —— 用户会去查根本不存在的配置问题"
    )


def test_reap_stale_runs_is_idempotent(db_session):
    """收割幂等：重复调用不会再动已经收掉的行（启动钩子可能被多次触发）。"""
    from app.style.jobs import create_distill_run, reap_stale_runs

    p = Project(title="收割测试3", target_length=5000, mode="quick", status="created")
    db_session.add(p)
    db_session.commit()

    create_distill_run(db_session, p.id, TIER_FAST)
    assert reap_stale_runs(db_session) == 1
    assert reap_stale_runs(db_session) == 0
