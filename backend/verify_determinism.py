"""确定性验证：不同 PYTHONHASHSEED 下同一语料的抽取结果必须逐字节一致。"""
import hashlib, json, sys
sys.path.insert(0, ".")
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.kg.extractor import RuleBasedExtractor
from app.models import NovelChunk

eng = create_engine("sqlite:///novel_demo.db")
with Session(eng) as s:
    chunks = s.scalars(select(NovelChunk).order_by(NovelChunk.id)).all()
res = RuleBasedExtractor().extract(chunks)
payload = json.dumps({
    "chars": {n: (c.mentions, sorted(c.chapters)) for n, c in res.characters.items()},
    "rels": {f"{k[0]}|{k[1]}": sorted(v.chapters) for k, v in res.relationships.items()},
}, ensure_ascii=False, sort_keys=True)
print(f"seed={__import__('os').environ.get('PYTHONHASHSEED','random'):>6}  "
      f"角色 {len(res.characters)}  边 {len(res.relationships)}  "
      f"sha256={hashlib.sha256(payload.encode()).hexdigest()[:16]}")
