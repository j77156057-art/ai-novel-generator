# Judge 时间锚量纲转换设计（kg-judge-era-dimension）

> 2026-10-16。目标：补齐接手文档候选 #7——`impossible_sequences` 只判 4 位年份，
> 中文历史时空的时间表述（「贞观三年秋」）完全不参与比较。
> 设计原则与 Judge 定位一致：**只报告、不改正**，宁漏勿误。

## 一、根因（带代码定位）

`backend/app/kg/judge.py:316-348` `check_impossible_sequences`：

```python
m = re.search(r"(19|20)\d{2}", ev.in_story_date or "")
if not m:
    continue        # ← 「贞观三年秋」在这里被跳过
```

年份量纲只有 4 位公元年一种。而 extractor prompt 明确引导 LLM 输出
故事内时间原句（`extractor_v3.py:301`：`"in_story_date": "故事内时间（如贞观三年秋）"`），
所以**中文历史时空项目的 in_story_date 全部落在 continue 分支**——规则形同虚设。

## 二、现状资产盘点

已有：`KgEvent.in_story_date` VARCHAR(64)（models.py:286，注释就写着「物理时间校验的关键输入」）；
`KgEvent.sequence`（项目内全局事件序号，递增即叙事顺序）。

本次新建：

- `backend/app/kg/data/era_names.json` —— 年号→公元年起止表（449 年号/520 记录/43KB）
- `backend/app/kg/era_convert.py` —— 纯函数式解析层（无 DB 依赖）
- `backend/app/kg/judge.py` 改造 `check_impossible_sequences`

## 三、数据源与构建（已实测完成）

**来源**：台湾教育部《重编国语辞典修订本》「历代皇帝年号表」
（dict.revised.moe.edu.tw appendix ID=43），按 page 1-6 抓取，521 行年号记录。

**实测踩过的两个解析坑**（都已修）：

1. **年份数字粘连**——HTML 单元格 `建元 B.C.140B.C.135` / `弘治 14881505`，
   正数年号直接粘成 6-8 位数字。切分策略：合法域 (1 ≤ y1 ≤ y2 ≤ 2100) 内
   枚举切点取 y2 最大者。B.C./A.D. 前缀转负号。**521 行 0 坏行**。
2. **繁体来源**——「貞觀」「元豐」「大觀」全是繁体，需 opencc 繁→简。
   装 `opencc-python-reimplemented`，注意 `OpenCC("t2s")` 不带 `.json` 后缀
   （带后缀会 FileNotFoundError，库的 bug）。

**产物**：`era_names.json` = `{年号: [{start, end, dynasty}, ...]}`，56 个年号同名多处
（如「建元」= 西汉 -140 / 东晋 343 / 南齐 479）——**表必须支持一年号多记录**。

## 四、解析层设计（era_convert.py）

### 4.1 输入输出契约

```python
def parse_story_time(text: str) -> int | None:
    """'贞观三年秋' → 629（贞观 627-649，元年=start）；'2020年3月' → 2020。
    无法解析 → None。只认年份级精度，月日/季节一律丢弃。"""
```

### 4.2 双层解析

**第一层：公元年**（现有行为保持）——`re.search(r"(19|20)\d{2}")`。
**第二层：年号纪年**（新增）：

```
regex: (年号表键) [一二三四五六七八九十百零〇\d]{1,4} 年?
中文数字: 元/正/1 → 元年；cn2int("三")=3；"十一"=11；不支持「元年三月」细分
公元年   = era.start + (纪年 - 1)      # 贞观三年 = 627 + 2 = 629
```

「十」单独出现解析为 10（省略口令「一十」）。超长数字（>4 位）不解析。

### 4.3 误报防线（红楼梦实测：宽松正则 60 命中中 56 个假阳性）

**坑**：裸年号正则 `(贞观|康熙|...)` 会把非年号词切进来。红楼梦全文实测 15 处命中
**全部是假阳性**，按成因分四类：

| 成因 | 样例 | 防线 |
|---|---|---|
| 跨词切碎（正则贪左侧） | 老太太**太和二**奶奶（4 处）/ 总理**元始三**一教门 | **左边界**：年号前只能句首/标点/朝代号 |
| 量词误食（年号后数字其实是量词） | 得了**凤凰一般** / **如意一**柄 / 斩**黄龙一**线 | **右边界**：数字后必须接「年」或季节词/标点/句尾 |
| 著录语境（句子抠字段） | 宋元丰五年四月眉山苏轼见于秘府 | **长度防线**：字段值 ≤10 字符（DB 实测真实值全 ≤7） |
| 园林/建筑后缀 | 大观园试才题对额 | 右边界已覆盖（「园」非季节词） |

**实现底线**：三道边界规则，全部 fail-closed，**不堆语义黑名单**
（「苏轼/书/题/铭」这类词表太脆，新人名/新器物照样漏）。
实测：22 个用例（10 真阳性 + 12 假阳性）全过，红楼梦全文 0 误报。

**不要在正则里省 `元/正`**——「康熙元年」的数字位是「元」不是
`[一二三四五六七八九十...]`，漏掉整个匹配消失。

### 4.4 同名年号消歧

## 五、judge.py 改动：相邻同 era 口径（口径 C）

**用户决策（2026-10-16 修订，取代原投票方案）**：不做「主年号投票」。
理由——实测证明换年号链的公元年必然单调不降（贞观23→永徽1→永徽6 = 649→650→655，
康熙61→雍正1 = 1722→1723，共 6 组真实链全部成立），**改元本身就是时间推进**，
所以跨年号比大小理论安全，不需要投票门槛来回避。

三口径在混排数据（贞观/永徽/贞观 + 两处真倒挂）上的对比：

| 口径 | 检出真倒挂 | 参数 |
|---|---|---|
| A 投票同 era 比（原方案） | 漏 1 处 | k=3 + threshold=2 两个任意参数 |
| B 投票+异 era 自比 | 全 | 仍带投票 |
| **C 相邻同 era 比（定稿）** | **全** | **零参数** |

**实现**（judge.py:316 起，替换年份判定部分）：

```python
# 1) 事件按 sequence 排序（乱序输入会全漏，必须排序）
events = db.scalars(select(KgEvent).where(project_id == ..., in_story_date.isnot(None))
                    .order_by(KgEvent.sequence)).all()

# 2) 每个事件解析 (era, year)；era=None 表示公元年形态
tl = [(parse_story_time(ev.in_story_date), ev) for ev in events]

# 3) 相邻同 era 才比 year（era 相同的连续事件）
prev = None                      # (era, year, sequence, summary)
for year, ev in tl:
    if year is None:
        prev = None              # 解析失败 → 断开比较链（不猜）
        continue
    era = extract_era_name(ev.in_story_date)
    if prev is not None and prev[0] == era and year < prev[1]:
        findings.append(...)
    prev = (era, year, ev.sequence, (ev.summary or "")[:40])
```

**三个保守边界**：

1. **解析失败即断链**——任一事件 `parse_story_time` 返回 None，该事件之后的
   比较重新开始（不从上一个成功事件继承）。宁漏勿错。
2. **跨 era 不比**——`prev[0] != era` 时只更新 prev 不报警。换年号是正常推进。
3. **同名年号消歧**——56 个年号在多个朝代重复使用（「建元」= 西汉 -140 / 东晋 343 /
   南齐 479）。`era_year` 取**公元年落在该年号起止区间内**的那条记录；多条都容
   （极少）取最小公元年；都不容 → None。架空/复用年号因此安全退回「不比」。

## 六、复用与不改的东西

- 不改 `KgEvent` schema（in_story_date 已是 64 字符文本）
- 不改 extractor prompt（已引导输出「贞观三年秋」形态）
- 不改 RULES dict（impossible_sequences 的 severity/描述不变）
- 前端 Judge 面板已渲染所有 findings，无改动
- 不受影响的规则：teleportation / unauthorized_knowledge / secret_leak /
  foreshadow_expiry / language_mix —— 只动一个函数

## 七、测试计划（新文件 tests/test_era_dimension.py）

1. `parse_story_time`：公元年 3 例（2020/带月/无匹配）、年号 6 例
   （贞观三年→629 / 康熙元→1662 / 大观四年→1110 / 嘉靖→None(超区间) /
   元丰五年→1082 / 不含「年」的裸年号→None）
2. 四类假阳性逐条断言：跨词（老太太和二奶奶）/ 量词（如意一柄、黄龙一线）/
   著录长句（宋元丰五年四月眉山苏轼见于秘府）/ 园林（大观园试才题对额）
3. 同名年号消歧：建元 6 年（西汉 -135）/ 建元 3 年（东晋 345）/ 建元 10 年→None（超所有区间）
4. `check_impossible_sequences` 口径 C：
   - 同 era 倒挂报 err（seq1 贞观三年 629 → seq2 贞观元年 627）——**本次核心增量**
   - 同 era 递增不报
   - **跨 era 换年号不报**（贞观元年→永徽元年，629→650 递增天然不报）
   - **混排贞观/永徽/贞观 两处真倒挂全检出**（口径 A 会漏 1 处）
   - 解析失败断链：seq1 贞观三年（629）→ seq2「时间不明」→ seq3 贞观元年（627）不报
   - 旧行为保留：4 位公元年递减报 err / 递增不报
5. 红楼梦全量回归：`data/seed/红楼梦.txt` 逐 ±4 字窗口扫 `parse_story_time` 0 命中（防假阳性）
6. 既有 29 断言 test_v30_judge.py 必须全绿——其中「时间穿越：非年份时间不判」
   断言要改成「贞观三年 vs 贞观元年现在判」，这是**测试期望值变更，不是实现回退**

## 八、工作量与顺序

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | era_names.json（数据） | 已落地，449 年号 |
| 2 | era_convert.py + 单测 | parse 用例全过 |
| 3 | judge.py 改造 + 测试 | 29+ 旧断言绿 |
| 4 | 红楼梦全量回归 | 0 命中确认 |
