# 模型渠道失败时，界面上要说真话

> 2026-10-29 在浏览器里点「生成体系」点出来的问题。
> 不是新功能，是**失败路径**没写。

## 现象

点「生成体系」，面板里出现一行红字：

```
Internal Server Error
```

后端栈是：

```
RuntimeError: MiniMax chat 调用失败: HTTP Error 429: Too Many Requests
```

也就是说 —— **渠道限流被当成了本站 500**。

## 为什么前面所有检查都没抓到

单元测试、类型检查、`vue-tsc --noEmit`、`npm run build`、CI 全绿。

因为**成功路径是通的**：请求发出去了、参数对了、解析逻辑也对了。
出问题的只是「模型这次没回来」时的那个出口 —— 而那个出口从来没被走过。

同类教训在这个项目里已经有过两次（见 `ENGINEERING.md`）：
「应用到本项目」按钮因为漏了 `Content-Type` 从来没通过；
「编辑剧情圣经」弹窗因为 CSS 少一个花括号从来没显示过。
**都是代码看着对、测试全绿，只有人真的点一次才现形。**

## 两层根因

### 第一层：端点没有异常出口

`POST /api/projects/{id}/power-system/generate` 里直接：

```python
raw = provider.chat(...)          # 渠道一抖就抛
```

FastAPI 接住未知异常 → 500 → 响应体 `{"detail": "Internal Server Error"}`。
前端把 `detail` 原样显示（这个行为是对的），于是人看到的就是那句英文。

受影响的端点一共 6 个，全部是「AI 生成」这一类：

| 端点 | 这件事 |
|---|---|
| `character-cards/generate` | 补全角色卡 |
| `character-cards/batch` | 批量生成角色卡 |
| `power-system/generate` | 生成修炼体系 |
| `power-system/tiers/generate` | 生成档位分层 |
| `cultivation/plan` | 规划修炼路线 |
| `scale/generate` | 给角色定量级 |

### 第二层（更隐蔽）：生成器把渠道故障说成「模型没给出可用结果」

`character_card_gen.complete_card` 里：

```python
except Exception:            # noqa: BLE001 —— 完善失败不该让用户写的东西丢掉
    return {}
```

这条 `except` 的初衷是对的：**完善失败不该让用户已经写好的东西丢掉**。
但它把 429 也一起吞了，端点于是返回：

```json
{"ok": false, "detail": "AI 没给出可用结果（可重试或改写要求）"}
```

**这是把渠道故障说成模型不听话。** 人会照着提示一直改要求，
改到天荒地老也不会好 —— 因为问题根本不在要求上。

## 修法

### 1. 统一出口：`_model_call(what, fn)`

```python
def _model_call(what: str, fn):
    try:
        return fn()
    except HTTPException:
        raise                      # 端点自己的业务判断，不许被改写
    except Exception as exc:
        raise HTTPException(502, detail=_model_failure_detail(what, exc)) from exc
```

`what` 是**这件事的名字**（「生成修炼体系」），直接进界面文案。

### 2. 为什么是 502，不是 500

500 的含义是「本站写错了」。渠道限流 / 超时 / 欠费是**外部状态**，
人应该据此判断「等会儿再试」还是「去配额度」。
用错状态码，前端就没法区分「该重试」和「该报 bug」。

### 3. 文案口径

| 渠道说什么 | 界面说什么 |
|---|---|
| `429` / `Too Many Requests` | 模型渠道限流或额度已用尽 |
| `FreeUsageLimitError` / `insufficient` / `quota` | 模型渠道额度已用尽 |
| `401` / `403` / `unauthorized` | 模型渠道鉴权失败 |
| `timed out` / `timeout` | 模型渠道超时 |
| 其它 | 模型渠道调用失败 |

末尾一律附**渠道原话**（截到 200 字）：

> 生成修炼体系没做成：模型渠道限流或额度已用尽。渠道原话：MiniMax chat 调用失败: HTTP Error 429: Too Many Requests

放整段栈没用（看不懂），但也不能把原话吞掉 —— 它是判断「重试还是充值」的唯一线索。

判断顺序有意义：先认限流，再认鉴权，最后才认超时
（「HTTP Error 429」里没有 timeout 字样，但反过来容易误伤）。

### 4. `raise_errors=True`：只在人亲手点了按钮的地方抛

给三个生成器加可选开关，**默认行为一个字不变**：

- `complete_card(..., raise_errors=False)`
- `generate_cards(..., raise_errors=False)`
- `plan_route(..., raise_errors=False)`

端点传 `True` ⇒ 渠道异常往上走，被 `_model_call` 翻成 502。
后台/可选路径（KG 抽卡、蒸馏时顺手完善）继续吞 —— 那里的诉求确实是「别阻断主链」。

**边界**：这不是「以后都抛」。是「用户点了按钮，他要原因」。

## 钉住的测试（`tests/test_model_call_error_surface.py`，14 项）

- 文案：限流 / 额度 / 鉴权 / 超时 / 认不出 / 截断 / 换行压平；
- `_model_call`：成功原样返回；失败是 **502 不是 500**；端点内部 4xx 不被改写；
- 端点级真打一次请求：渠道 429 → 502 + 中文原因（并断言**不是** `Internal Server Error`）；
- 端点级成功路径：包了一层之后 200 + 解析结果不变；
- **结构守门**：`projects.py` 里任何 `provider.chat(` 都必须裹在 `_model_call(...)` 里
  —— 以后新加端点忘了裹，这条测试就红。

## 已知限制

- `_MODEL_FAILURE_HINTS` 是**字符串匹配**，渠道换了报错措辞就会落到「模型渠道调用失败」
  这一档。它不会说错话，只是说得不够具体 —— 原话仍在末尾。
- 502 在 `test` 环境里不会自动重试。要不要自动重试一次（429 常是瞬时的）
  留作后续；现在先把「说清楚」做到位。

## 同一个毛病的第三处：错误写在了人看不到的地方

走查量级弹窗时发现：在弹窗里点「AI 帮我定」失败，代码把原因写进 `pwErr`，
而 `pwErr` 只渲染在**弹窗背后的面板**上。弹窗盖着面板 —— 人什么也看不到，
只觉得「点了没反应」，然后反复点。

修法：把 `pwErr` 的提示同时摆进两个弹窗（量级 / 修炼档案），
并在打开弹窗时清掉面板上的旧错误（否则会看到上一次的残留）。

**口径**：一处状态可以被多个位置读，但**至少要有一个位置在人当下看得到的地方**。
