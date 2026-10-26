# 真链路 percentile 100% 根因根治：方案 B「对数粒度 profile」

> 设计时间：2026-10-17 16:50
> 背景：8 轮真链路冒烟 + 1 轮 probe + 1 轮并行 v2.12 实测一致确认 `style_distance` 粒度不对齐（详见 `docs/percentile_root_cause_analysis.md`）

---

## 0. 现状问题

`fit_profile(unit_chars=3000)` 把参考切 3000 字/单元统计 mean/std。生成段 6264 字（2.09x）或 2841 字（0.95x）时，`features` 在不同长度上算密度（省略号/100字、句号/100字、虚词/100字）—— 3000 字留出集统计的 mean/std **不能直接对 6000 字段算 z**—— `mean_abs_z` 系统性偏大—— `percentile = 100%`。

**关键证据** —— `style_distance` line 481 自身注释：
> 粒度不对齐时距离会系统性偏大，必须如实标注而不是静默给个数字
> `scale_ratio`: round(len(text) / unit_chars, 2) if unit_chars else None

**但代码只标注 scale_ratio，不修正**。

---

## 1. 方案 B 核心思路

**Profile fit 时按对数粒度切**（unit_chars/2 + unit_chars + unit_chars*2 三套）：
- 短文（`len < 1.5 * unit_chars`）→ 走 `unit_chars` profile
- 长文（`1.5 <= len/unit_chars < 3`）→ 走 `unit_chars*2` profile
- 超长文（`len >= 3 * unit_chars`）→ 走 `unit_chars*4` profile 或更激进切
- 短文（`len < 0.5 * unit_chars`）→ 走 `unit_chars/2` profile

**生成段按 length 选最匹配 profile** —— 解决粒度不对齐。

**为什么用对数粒度而非 1:1 重切**：
- 实际生产中**段长分布不均**（智谱 1300-6300 / nemotron 600-3700 / 用户手改或大模型 8000+）
- 1:1 重切要求生成段 ≈ unit_chars（3000），**与 prompt 的「写不少于 0.8N 字」+ max_tokens 4096 撞**—— 段长永远波动
- 对数粒度（0.5x / 1x / 2x）= 3 档覆盖，**对工程侵入最小**

---

## 2. 数据结构

### 2.1 profile schema 升级

原：
```python
profile = {
    "version": 1,
    "unit_chars": 3000,
    "n_train": 62, "n_holdout": 27,
    "feat": {特征: {mean, std, p10, p50, p90, min, max}},
    "holdout": {
        "mean_abs_z": {...},
        "unit_mean_abs_z": [...],
    },
    "fingerprint": {...},
}
```

新：
```python
profile = {
    "version": 2,  # 升级
    "label": "...",
    "fingerprint": {...},  # 包含 schema + bucket 列表
    "buckets": {
        # 3 套 profile，按 unit_chars 分
        "short": {  # unit_chars = 1500
            "unit_chars": 1500,
            "n_train": ..., "n_holdout": ...,
            "feat": {...},
            "holdout": {...},
        },
        "medium": {  # unit_chars = 3000（主档，等价旧版）
            "unit_chars": 3000,
            "n_train": ..., "n_holdout": ...,
            "feat": {...},
            "holdout": {...},
        },
        "long": {  # unit_chars = 6000
            "unit_chars": 6000,
            "n_train": ..., "n_holdout": ...,
            "feat": {...},
            "holdout": {...},
        },
    },
}
```

**schema 升级 = 老 profile 自动失效**（fingerprint 包含 schema 字段，不相等 → 重算）。

### 2.2 选 bucket 规则

```python
def pick_bucket(profile: dict, text_chars: int) -> tuple[str, dict]:
    """按 text 长度选最匹配的 profile bucket。"""
    base = profile["_meta"]["base_unit_chars"]  # 即 generation_length
    if text_chars < 0.6 * base:
        return "short", profile["buckets"]["short"]
    elif text_chars < 1.7 * base:
        return "medium", profile["buckets"]["medium"]
    else:
        return "long", profile["buckets"]["long"]
```

**容差**：
- `text < 0.6 * unit_chars`（如 1800 字 vs 3000）走 short（1500）
- `0.6 <= text/unit_chars < 1.7`（如 1800-5100 vs 3000）走 medium（3000）
- `text >= 1.7 * unit_chars`（如 5100+ vs 3000）走 long（6000）

---

## 3. 关键路径改动

### 3.1 `app/style/stylometry.py` —— fit_profile 升级

```python
def fit_profile(
    texts: Iterable[str],
    *,
    unit_chars: int = 3000,
    train_ratio: float = 0.7,
    max_units_per_text: int | None = 400,
    label: str = "",
) -> dict:
    """对数粒度 profile fit：3 套 unit_chars（base/2, base, base*2）。"""
    # 1. 切 3 套单元
    buckets_units = {
        "short": [],   # unit_chars=base/2
        "medium": [],  # unit_chars=base
        "long": [],    # unit_chars=base*2
    }
    bucket_units = {
        "short": base := max(500, int(unit_chars * 0.5)),
        "medium": unit_chars,
        "long": int(unit_chars * 2),
    }
    for t in texts:
        for name, uc in bucket_units.items():
            for u in cut_units(t, uc, max_units=max_units_per_text):
                buckets_units[name].append((label, u))

    # 2. 对每套 unit_chars 单独 fit
    profile = {
        "version": 2,  # 升级
        "label": label,
        "buckets": {},
        "_meta": {
            "base_unit_chars": unit_chars,
            "schema": "log_buckets_v1",
        },
    }
    for name, units in buckets_units.items():
        if len(units) < 4:
            # 短语料：某个 bucket 切不出 4 个单元——降级用相邻 bucket
            continue
        bucket_profile = _fit_single_bucket(units, bucket_units[name], train_ratio)
        profile["buckets"][name] = bucket_profile

    # 至少要有 medium bucket
    if "medium" not in profile["buckets"]:
        # 极端 fallback：单一 bucket 模式（等价旧版）
        ...

    return profile
```

### 3.2 `app/style/stylometry.py` —— style_distance 用 bucket 选 profile

```python
def style_distance(text: str, profile: dict) -> dict:
    """按 text 长度选 profile bucket 算 distance。"""
    vec = features(text)
    bucket_name, bucket_profile = pick_bucket(profile, len(text))
    z = z_scores(vec, bucket_profile)
    abs_z = {k: abs(v) for k, v in z.items()}
    total = statistics.fmean(list(abs_z.values())) if abs_z else 0.0

    hold = bucket_profile.get("holdout", {})
    dist = hold.get("unit_mean_abs_z") or []
    percentile: float | None = None
    if dist:
        below = sum(1 for d in dist if d <= total)
        percentile = 100.0 * below / len(dist)

    return {
        "chars": len(text or ""),
        "unit_chars": bucket_profile.get("unit_chars"),
        "bucket": bucket_name,
        "scale_ratio": round(len(text or "") / (profile["_meta"]["base_unit_chars"] or 1), 2),
        "mean_abs_z": round(total, 3),
        "max_abs_z": round(max(abs_z.values()), 3) if abs_z else 0.0,
        "percentile": None if percentile is None else round(percentile, 1),
        ...
    }
```

### 3.3 `app/style/pipeline.py` —— fingerprint 升级

```python
def profile_fingerprint(source_ids: list[int], unit_chars: int) -> dict:
    return {
        "source_ids": sorted(int(i) for i in source_ids),
        "unit_chars": int(unit_chars),
        "version": PROFILE_VERSION,  # 升到 2
        "schema": "log_buckets_v1",  # 新字段
    }
```

老 profile (`schema: "single_v1"`) fingerprint 不匹配 → 自动失效重算。

### 3.4 `verify_stylometry.py` —— 验收脚本同步

参考 `style_distance` 升级：把 `pick_bucket` 抽出来共享使用。

---

## 4. 风险评估

| 风险 | 缓解 |
|---|---|
| 切 3 套 unit 耗时 3x | 一次性 fit + 缓存 fingerprint；只在 source_ids/unit_chars 变时重算 |
| 短语料（< 4 单元）某 bucket 切不出 | 降级：跳过该 bucket；`pick_bucket` 走相邻 bucket |
| 老 profile 失效，全量重算 | 一次性，background 跑——已有 `force=False` 缓存机制 |
| percentile 阈值（97.5）需重新校准 | v2.7-v2.10 数据可重跑：参考 `docs/v21-v211_*_smoke_*.json` raw 数据，对比 percentile 分布 |
| 6 轮真链路冒烟 raw data 仍可重跑 | 9 段 warning 已落库 `projects.style_narrative` JSON 字段；engine.py:920-926 append_style_warning 路径不需改 |

---

## 5. 实施计划

1. **Step 1（半天）**：建 `tests/test_log_buckets_profile.py`（20 断言）
   - 短/中/长 bucket 切出 N 单元验证
   - fingerprint 升级（schema 字段）→ 老 profile 失效
   - `pick_bucket` 选 bucket 规则验证
2. **Step 2（半天）**：`app/style/stylometry.py` 实现 fit_profile v2 + style_distance v2
3. **Step 3（半天）**：`app/style/pipeline.py` fingerprint + project_profile 同步
4. **Step 4（半天）**：跑 14 套件确认不破 + 重跑 v2.10 + v2.12 验证 percentile 改善
5. **Step 5（半天，可选）**：98c6461 已加 Style 审计 subtab，前端展示 bucket 字段（让用户看到「这条 segment 走的是 long bucket」）

**总估时 2 天**（你「精巧」偏好下，**Step 1-4 必做**，Step 5 可选）。

---

## 6. 不做什么（精简范围）

- ❌ 不改 `cut_units` 本身（cut_units 现有 `floor = max(1, unit_chars // 2)` 已能切小单元）
- ❌ 不改 features 提取（features 仍是密度类，依赖 len 归一化由 bucket 解决）
- ❌ 不动 max_tokens 修复（75d481a 已 commit，bucket 切分独立）
- ❌ 不动 prompt 强化（cf93ccb / e51e042 已 commit，bucket 切分独立）

---

## 7. 设计哲学（沿用用户偏好）

- **审计先行**——开新维度前先查 master 现状（已查清 fit_profile / style_distance / pipeline.py）
- **精巧优先**——3 套 bucket 覆盖 0.5x-2x 范围，比 1:1 重切更省；不引入新表/新字段到 DB
- **设计先行、过审再开工**——本设计文档先评审再动 `stylometry.py`
- **失败用 probe 不急改实现**——根因 5 次验证（v2.7-v2.10 + v2.12 + probe + style_distance 注释 + features 密度）才动手
