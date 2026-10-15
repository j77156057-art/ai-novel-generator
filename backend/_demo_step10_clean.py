"""一次性脚本：在**临时库**上演示「第 10 步 Agent 辅助语料清洗」整链路。

演示内容（全走真实 HTTP 端点，不碰真实库）：
  1. `ingest_text` 导入一小段**人造脏文本**（含乱码 / 断字 / 重复字），
     并展示入库时记录的「待复核可疑点数量」（clean_report.text_clean）；
  2. `POST /api/assistant/clean-corpus` 检测 + 复核 → 展示检出的 issues 与
     产出的 pending 提案（before/after）；
  3. `POST /api/assistant/proposals/{id}/approve` + `/apply` 落库 →
     展示 `novel_chunks.text` 前后对照；
  4. `POST /api/assistant/proposals/apply-selected` 受控批量 + 通配符被拒。

不碰真实开发库：DATABASE_URL 必须由外部指向临时库（脚本自检并拒绝非临时库）。

用法（在 backend/ 目录下）：
    DATABASE_URL="sqlite:///D:/Temp/demo_step10.db" python _demo_step10_clean.py
"""
from __future__ import annotations

import json
import os
import sys

_url = os.environ.get("DATABASE_URL", "")
if "novel_dev.db" in _url or "novel_demo.db" in _url or not _url:
    raise SystemExit(
        f"拒绝运行：DATABASE_URL={_url!r} 未指向临时库。"
        "请显式设置到临时目录（如 D:/Temp/demo_step10.db）。"
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.ingestion.store import ingest_text  # noqa: E402
from app.models import AgentRun, NovelChunk, Project, RawNovel  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

# 人造脏文本：乱码替换符 + 断字（句子被硬换行切断）+ 相邻重复字。
DIRTY = (
    "第一段：他他走进了那间堆满旧书的小屋，看到门牌上写着「欢迎」。\n"
    "第二段：这里本来是一句话，但编码转换失败留下了乱码\ufffd\ufffd\ufffd然后继续往下读。\n"
    "第三段：他推开门走进了那间堆满旧书的小屋然后\n"
    "径直走向窗边那张落了灰的木桌旁边，慢慢地坐了下来。\n"
    "第四段：的的重复字测试，还有一句正常的话。\n"
    "第五段：风雪夜，陆见推开木门，看向远方。\n"
)


def main() -> int:
    init_db()
    # 缩小分片，把上面的文本切成几片，便于演示「多提案 + 批量应用」。
    settings.chunk_size = 60
    settings.chunk_overlap = 0

    with SessionLocal() as db:
        proj = Project(title="第10步清洗演示项目", target_length=5000,
                       mode="quick", status="created", reference_novel_ids=[])
        db.add(proj)
        db.flush()
        res = ingest_text(db, DIRTY, title="人造脏文本.txt", platform="upload")
        novel = db.get(RawNovel, res["novel_id"])
        project_id = int(proj.id)
        novel_id = int(novel.id)
        tc = (novel.clean_report or {}).get("text_clean", {})
        chunks = db.query(NovelChunk).filter(NovelChunk.novel_id == novel_id) \
            .order_by(NovelChunk.id).all()
        before = {int(c.id): c.text for c in chunks}
        db.commit()

    print("=" * 72)
    print("[1] 入库：人造脏文本 → 分片")
    print(f"    novel_id={novel_id} project_id={project_id} 分片数={res['chunks']}")
    print(f"    clean_report.text_clean = {json.dumps(tc, ensure_ascii=False)}")

    with TestClient(fastapi_app) as client:
        print("=" * 72)
        print("[2] POST /api/assistant/clean-corpus （检测 + 复核，不写正文）")
        resp = client.post("/api/assistant/clean-corpus",
                           json={"project_id": project_id, "novel_id": novel_id,
                                 "limit": 20})
        body = resp.json()
        print(f"    HTTP {resp.status_code}  answer: {body['answer']}")
        with SessionLocal() as db:
            used_model = db.get(AgentRun, body["agent_run_id"]).model
        print(f"    Assistant provider 实际使用：{used_model!r}")
        print(f"    model_participated={body['model_participated']} "
              f"model_note={body['model_note']}")
        print(f"    检出 issues 数：{len(body['issues'])}")
        for i in body["issues"]:
            print(f"      - [{i['severity']}] {i['rule']} | {i['claim'][:70]}")
        print(f"    suggested_edits 数：{len(body['suggested_edits'])}  "
              f"pending 提案数：{body['proposal_count']}")
        pids = [p["id"] for p in body["proposals"]]
        for p in body["proposals"]:
            print(f"      · proposal#{p['id']} chunk={p['target_id']} "
                  f"status={p['status']} risk={p['risk_level']}")
            print(f"        before: {p['before_text'][:44]!r}")
            print(f"        after : {p['after_text'][:44]!r}")

        # 校验：此刻 novel_chunks 尚未被改
        with SessionLocal() as db:
            now = {int(c.id): c.text for c in db.query(NovelChunk)
                   .filter(NovelChunk.novel_id == novel_id).all()}
        print(f"    [校验] 创建提案后 chunk 是否被改动: {now != before}（应为 False）")

        if not pids:
            print("    （无提案可应用，结束）")
            return 0

        print("=" * 72)
        print("[3] 逐条 approve → apply（第一批 1 条），展示 chunk 前后对照")
        pid = pids[0]
        client.post(f"/api/assistant/proposals/{pid}/approve",
                    json={"action": "approve", "actor": "demo"})
        r = client.post(f"/api/assistant/proposals/{pid}/apply",
                        json={"action": "apply", "actor": "demo"})
        out = r.json()
        print(f"    HTTP {r.status_code} status={out.get('status')} "
              f"kind={out.get('kind')} version_id={out.get('version_id')}")
        cid = int(out["target_id"])
        print(f"    chunk {cid} before: {before[cid]!r}")
        with SessionLocal() as db:
            after_text = db.get(NovelChunk, cid).text
            emb = db.get(NovelChunk, cid).embedding
        print(f"    chunk {cid} after : {after_text!r}")
        print(f"    embedding 置空（旧向量失效）: {emb is None}")

        print("=" * 72)
        print("[4] 受控批量应用：apply-selected")
        # 先看通配符被拒
        bad = client.post("/api/assistant/proposals/apply-selected",
                          json={"action": "apply", "all": True})
        print(f"    通配符 {{'all': true}} → HTTP {bad.status_code}（应为 422）")
        over = client.post("/api/assistant/proposals/apply-selected",
                           json={"action": "apply",
                                 "proposal_ids": list(range(1, 200))})
        print(f"    超上限（199 条）→ HTTP {over.status_code}（应为 422）")
        rest = pids[1:]
        for extra in rest:
            client.post(f"/api/assistant/proposals/{extra}/approve",
                        json={"action": "approve", "actor": "demo"})
        if rest:
            bulk = client.post("/api/assistant/proposals/apply-selected",
                               json={"action": "apply", "proposal_ids": rest,
                                     "actor": "demo"})
            bo = bulk.json()
            print(f"    批量应用 {rest} → HTTP {bulk.status_code} ok={bo.get('ok')} "
                  f"applied={len(bo.get('applied', []))}")
            with SessionLocal() as db:
                for e in bo.get("applied", []):
                    c2 = int(e["target_id"])
                    print(f"      · chunk {c2} after: {db.get(NovelChunk, c2).text!r}")

    print("=" * 72)
    print("[done] 临时库：", os.environ["DATABASE_URL"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
