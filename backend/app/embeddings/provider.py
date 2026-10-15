"""Embedding 提供方。

- DashScopeProvider：通义千问 text-embedding-v3（OpenAI 兼容端点），输出维度可配（默认 1536，匹配现有 Vector 列）。
- LocalFallbackProvider：确定性哈希词袋向量，仅用于开发/测试离线跑通；质量远低于真实模型，禁止用于生产。

工厂 get_embedding_provider() 按配置选择；未配置 key 时自动降级到本地兜底，保证离线可运行。
"""

import hashlib
import json
import math
import urllib.request

from app.config import settings
from app.tokenize import tokenize


class EmbeddingProvider:
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - 接口
        raise NotImplementedError


class DashScopeProvider(EmbeddingProvider):
    def __init__(self, api_key: str, base_url: str, model: str, dim: int) -> None:
        if not api_key:
            raise ValueError("DashScope 需要 DASHSCOPE_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        payload = json.dumps(
            {
                "model": self.model,
                "input": texts,
                "dimensions": self.dim,
                "encoding_format": "float",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"DashScope embedding 调用失败: {exc}") from exc
        items = sorted(data["data"], key=lambda x: x["index"])
        return [it["embedding"] for it in items]


class LocalFallbackProvider(EmbeddingProvider):
    """确定性哈希词袋向量（开发/测试用，非生产质量）。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._hash_vec(t) for t in texts]

    def _hash_vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


_PROVIDER: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    if settings.embedding_provider == "dashscope" and settings.dashscope_api_key:
        _PROVIDER = DashScopeProvider(
            settings.dashscope_api_key,
            settings.dashscope_base_url,
            settings.embedding_model,
            settings.embedding_dim,
        )
    else:
        # 未配置 key 或显式 local：降级本地兜底，保证离线可跑
        _PROVIDER = LocalFallbackProvider(settings.embedding_dim)
    return _PROVIDER
