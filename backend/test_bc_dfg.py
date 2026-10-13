"""B/C/D/F/G 的 HTTP 契约回归，使用独立 sqlite 库。"""

import os
import sys
import tempfile

DB_PATH = os.path.join(tempfile.gettempdir(), "ai_novel_bc_dfg_api.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app.main import app
from app.db import SessionLocal
from app.kg.build import build_graph_from_text


def main() -> int:
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "title": "契约测试", "premise": "用户基调锚点" * 60,
            "generation_length": 3000, "target_total_chars": 100000,
        })
        assert created.status_code == 200, created.text
        pid = created.json()["id"]
        assert created.json()["target_total_chars"] == 100000

        bad = client.post(f"/api/projects/{pid}/characters", json={
            "characters": [{"name": "  ", "role": "男主"}],
        })
        assert bad.status_code == 422, bad.text
        saved = client.post(f"/api/projects/{pid}/characters", json={
            "characters": [{
                "name": "林深", "role": "男主", "motivation": "找妹妹",
                "arc_state": "由逃避转向承担", "status": "active",
            }],
            "relationships": [],
        })
        assert saved.status_code == 200, saved.text
        patched = client.patch(f"/api/projects/{pid}", json={
            "plot_bible": {"synopsis": "试验梗概", "characters": saved.json()["characters"], "relationships": [], "pacing": [{
                "segment": 1, "conflict": "妹妹留下的线索出现反转",
                "character_state": "林深开始怀疑盟友", "daily": False,
            }]},
        })
        assert patched.status_code == 200, patched.text
        bible = client.get(f"/api/projects/{pid}").json()["plot_bible"]
        assert bible["characters"][0]["name"] == "林深"

        plan = client.post("/api/generate-plan", json={"project_id": pid}).json()
        assert "林深" in plan["upcoming_characters"][0]["name"]
        assert "目标 100000 字" in plan["user_prompt"]
        assert "第 1 段" in plan["user_prompt"]
        assert "妹妹留下的线索出现反转" in plan["user_prompt"]
        assert "不额外安排日常" in plan["user_prompt"]
        assert "pacing" not in plan["system_prompt"]
        assert "小说大概内容（用户锚点）" in plan["system_prompt"]
        assert "用户基调锚点" in plan["system_prompt"]
        assert "用户基调锚点" not in plan["user_prompt"]

        confirmed = client.post(f"/api/projects/{pid}/upcoming-characters", json={
            "confirmed": [{"name": "林深", "role": "男主", "motivation": "找妹妹"}],
        })
        assert confirmed.status_code == 200, confirmed.text
        bible = client.get(f"/api/projects/{pid}").json()["plot_bible"]
        assert bible.get("synopsis") == "试验梗概"
        assert bible["confirmed_upcoming"][0]["name"] == "林深"
        plan = client.post("/api/generate-plan", json={"project_id": pid}).json()
        assert "即将出场角色（已由用户确认）" in plan["system_prompt"]

        db = SessionLocal()
        try:
            build_graph_from_text(db, pid, "林深说道：我会把妹妹带回来。")
        finally:
            db.close()
        graph = client.get(f"/api/projects/{pid}/kg?source=generated").json()
        node = next(n for n in graph["nodes"] if n["type"] == "character")
        assert node["props"]["role"] == "男主"
        assert node["props"]["motivation"] == "找妹妹"
        assert node["props"]["arc_state"] == "由逃避转向承担"

        print("PASS B/C/D/F/G contracts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
