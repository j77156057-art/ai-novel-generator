"""M3 API 层端到端验证：通过真实 HTTP 链路跑通项目→图谱→查询。

端点覆盖：上传语料 → 建项目 → build-kg → kg → character → reminders。
离线（embedding 本地兜底）。
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_m3_api.db")
if os.path.exists(_DB):
    try:
        os.remove(_DB)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fixtures import FIXTURE  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> int:
    content = FIXTURE

    failures = []
    with TestClient(app) as client:
        # 1) 上传种子语料
        r = client.post(
            "/api/novels/upload",
            files={"file": ("fixture.txt", content.encode("utf-8"), "text/plain")},
        )
        if r.status_code != 200:
            failures.append(f"upload 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        novel_id = r.json()["novel_id"]
        print("upload ->", r.json())

        # 2) 建项目
        r = client.post(
            "/api/projects",
            json={"title": "种子项目", "mode": "quick", "target_length": 5000},
        )
        if r.status_code != 200:
            failures.append(f"create_project 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        project_id = r.json()["id"]
        print("create_project ->", r.json())

        # 3) 建图谱
        r = client.post(f"/api/projects/{project_id}/build-kg")
        if r.status_code != 200:
            failures.append(f"build-kg 失败 {r.status_code}: {r.text}")
            print("\n".join(failures))
            return 1
        print("build-kg ->", r.json())

        # 4) 查图谱
        r = client.get(f"/api/projects/{project_id}/kg")
        graph = r.json()
        char_names = sorted(n["props"]["name"] for n in graph["nodes"] if n["type"] == "character")
        print("kg characters ->", char_names)
        if set(char_names) != {"林夜", "苏晚"}:
            failures.append(f"角色集合应为 {{林夜,苏晚}}，实际 {set(char_names)}")

        # 5) 查角色
        r = client.get(f"/api/projects/{project_id}/kg/character/林夜")
        lin = r.json()
        print("character 林夜 ->", lin)
        if lin.get("appearances") != [1, 2, 3]:
            failures.append(f"林夜出场应为 [1,2,3]，实际 {lin.get('appearances')}")

        # 6) 生成前提醒
        # 6a) 默认 source=generated：刚建完参考语料、还没生成过任何段落，
        #     此时提醒必须为空——参考语料的角色/悬念**不得**泄漏进生成约束（P0-1 的固化）
        r = client.get(f"/api/projects/{project_id}/reminders")
        rem_default = r.json()
        default_names = [c.get("name") for c in rem_default["character_constraints"]]
        default_qs = [h.get("question", "") for h in rem_default["unresolved_plotholes"]]
        print("reminders 默认(generated) ->", {"chars": default_names, "plotholes": default_qs})
        if default_names or default_qs:
            failures.append(
                f"未生成任何段落时，generated 侧提醒应为空，实际 角色{default_names} 坑{default_qs}"
            )
        if any("林夜" == n or "苏晚" == n for n in default_names):
            failures.append(f"参考语料角色泄漏进生成约束：{default_names}")

        # 6b) 显式查参考语料那一侧：未填坑应含「那光是什么」悬念
        r = client.get(f"/api/projects/{project_id}/reminders?source=novel:{novel_id}")
        rem = r.json()
        qs = [h.get("question", "") for h in rem["unresolved_plotholes"]]
        print("reminders (novel:%d) ->" % novel_id, qs)
        if not any("那光是什么" in (q or "") for q in qs):
            failures.append(f"参考语料未填坑应含「那光是什么」悬念，实际 {qs}")

    if failures:
        print("\n❌ M3 API 验证失败：")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ M3 API 层端到端验证通过：上传→建项目→建图谱→查询 全链路正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
