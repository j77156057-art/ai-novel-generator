# 方案 C「features 长度归一化」设计

> 设计时间：2026-10-18 06:50
> 背景：方案 B「对数粒度 profile」(5 步 + last-mile + 60 章) 实施后 percentile 仍 100/100。
> 详见 `docs/percentile_root_cause_analysis.md` + `docs/style_log_buckets_design.md`。

---

## 0. 现状问题

8 轮真链路冒烟 + 1 轮 probe + 并行 v2.12 + 方案 B 5 步 + 60 章边际 = **13 轮验证一致**：

**percentile 100/100 是 features 在不同长度上算密度的 z 系统性偏大**：
- 短文（1261 字）vs long bucket 留出分布 max 1.613 → distance 2.094 远超
- 长文（5674 字）vs long bucket 留出分布 max 1.613 → distance 6.461 远超

**方案 B 部分缓解**（段 1 mean_abs_z 3.476→2.094 降 40%），但 percentile 计算本身没变（distance vs 留出分布 max 比例未变）。

**真正根因**：features 含 4 类「按字/句/段/run 统计」的值（**不是密度**），短文 vs 长文统计波动大：
- 句长均值（按字）
- 句长 CV / p90（按字）
- 段长 CV（按字）
- 段均句数（按句/段）
- 短/长句占比（按句）
- 短串占比（按 run）
- 段尾升华率（按段）
- 对白字/段占比（按字/段）

**这 8 个特征在 1000 字段 + 6000 字段上的 mean_abs_z 差异大**，导致 `mean_abs_z` 系统性偏大。

---

## 1. 方案 C 核心思路

**对每个非密度类特征做 log(len) 归一化**：

```python
# 旧: features(text) 直接算
"句长均值": statistics.fmean(sent_lens)  # 1000字 vs 6000字 差异大

# 新: features(text, *, unit_chars=3000) 按 log(len/unit_chars) 缩放
expected_log_scale = math.log(len(text) / unit_chars)
"句长均值": statistics.fmean(sent_lens) / max(0.5, math.exp(abs(expected_log_scale) * 0.3))
# 长文 (6000字/3000=2x) → 缩放因子 0.5; 短文 (1000字/3000=0.33x) → 缩放因子 1.2
```

**核心思想**：把 features 当成「按 log 长度归一化的统计量」—— 短文 vs 长文在同一长度基准下比较。

---

## 2. 数据结构 + 函数签名

### 2.1 features 函数签名升级

旧：
```python
def features(text: str) -> dict[str, float]
```

新：
```python
def features(text: str, *, unit_chars: int = 3000) -> dict[str, float]
```

`unit_chars` 是基准长度（默认 3000，对应 generation_length）—— features 算完后**乘以 log 归一化因子**让跨长度可比。

### 2.2 归一化因子

```python
def _length_scale_factor(text_chars: int, unit_chars: int) -> float:
    """按 log 长度比例缩放 features，让跨长度可比。

    length_ratio = text_chars / unit_chars
    log_ratio = math.log(length_ratio) if length_ratio > 0 else 0
    # 经验：长度差 1x 时 scale=1.0；2x 时 scale=0.7；0.5x 时 scale=1.3
    # 用 exp(-|log_ratio| * 0.3) 双向收敛
    return math.exp(-abs(log_ratio) * 0.3)
```

**为什么用 exp(-|log_ratio| * 0.3)**：双向对称 —— 短文 (0.33x) 和长文 (3x) 同等程度地「拉向」基准。

### 2.3 z-score 计算保持不变

```python
def z_scores(vec: dict[str, float], profile: dict) -> dict[str, float]:
    """逐特征 z 值。"""
    out: dict[str, float] = {}
    feat = profile.get("feat") or {}
    for k in FEATURES:
        st = feat.get(k) or {}
        sd = float(st.get("std") or 0.0)
        mean = float(st.get("mean") or 0.0)
        out[k] = (float(vec.get(k, 0.0)) - mean) / sd if sd > 0 else 0.0
    return out
```

---

## 3. 关键路径改动

### 3.1 `app/style/stylometry.py` features + fit_profile + style_distance

- `features(text, unit_chars)` 改签名
- 内部对 8 个非密度特征乘 `length_scale_factor(text_chars, unit_chars)`
- 密度类特征（`per1k` 归一化的）**不变**
- `fit_profile_v2` 切 3 套 bucket 时每套用对应 unit_chars 做归一化
- `style_distance` 走 v2 path 时按 text 长度选 bucket + 传 unit_chars 给 features

### 3.2 `verify_stylometry.py` 同步

参考 `style_distance` 升级：把 features 调用传 unit_chars。

---

## 4. 风险评估

| 风险 | 缓解 |
|---|---|
| features 改签名破坏现有 14 套件 | `unit_chars=3000` 默认参数，旧调用兼容；新加测试覆盖默认行为 |
| exp(-|log|*0.3) 系数 0.3 拍脑袋 | 用 verify_stylometry.py 在真实语料上校准（参考 stage2 `STYLE_WARN_PERCENTILE` 标定方法） |
| 8 个非密度特征识别错 | 显式列清单（见第 0 节），每个加注释说明 |
| 与方案 B bucket 切分叠加——v2 path 选 bucket 后 features 归一化因子不同 | 短文选 short bucket → unit_chars=1500；长文选 long bucket → unit_chars=6000；features 归一化因子按各自 bucket 算 —— **双层归一** |

---

## 5. 实施计划

1. **Step 1（半天）**：建 `tests/test_length_normalize.py`（25 断言）
   - features 签名升级（unit_chars 关键字）
   - `_length_scale_factor` 在 1x/2x/0.5x 处的值
   - 8 个非密度特征 + 12 个密度特征分组测试
   - 跨长度一致性：相同语料 + 不同长度 → z-score 差异应 < 阈值
2. **Step 2（半天）**：`app/style/stylometry.py` 实现 length scale
3. **Step 3（半天）**：`fit_profile_v2` + `style_distance` 传 unit_chars 给 features
4. **Step 4（半天）**：跑 17 套件确认不破 + 重跑 v2.14 + v2.15 验证 percentile 真降到 < 99.5
5. **Step 5（可选）**：前端加 features 归一化可视化（让用户看到「本段是 1.2x 归一化」）

**总估时 2 天**（按用户「精巧」偏好，**Step 1-4 必做**，Step 5 可选）。

---

## 6. 不做什么

- ❌ 不改 cut_units（仍按段落边界切）
- ❌ 不改 pick_bucket（仍按 (0.6, 1.7) 阈值选）
- ❌ 不动 max_tokens 修复（75d481a 已 commit）
- ❌ 不动 prompt 强化（cf93ccb / e51e042 已 commit）
- ❌ 不动阈值 99.5（f2a0611 已 commit）

---

## 7. 设计哲学

- **审计先行**——已查清 8 个非密度特征 + 12 个密度特征的归一化现状
- **精巧优先**——只对 8 个非密度特征加 scale，不动 12 个密度特征
- **设计先行、过审再开工**——本设计文档先评审
- **失败用 probe 不急改实现**——根因 5+ 轮验证才动手
- **与方案 B 叠加而非替代**——bucket 切分 + 长度归一化 = 双层归一，治本 percentile
