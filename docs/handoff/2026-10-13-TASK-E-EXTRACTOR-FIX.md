# 任务 E · 抽取器 3 字名 / 姓氏 / 噪声护栏（独立 BUG）

> 状态：**可独立 worktree 开工**。基线 HEAD = `55a12a2`（派单 v2 已落），从这里拉 worktree。
> 工作目录：`/c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/`
> 验收目标：`verify_sentinel.py` 96/1 → **96/0**（G1b 转绿）+ `verify_acceptance.py` 14/0 不变 + `test_p0`/`test_ledger` 不退化

---

## 1. 背景与根因

`backend/app/kg/extractor.py` 是 M3.x 的抽取引擎。当前实测有 2 类漏检，**导致**：
- `verify_sentinel.py` G1b 红（PASS 96 / FAIL 1）
- `novel_dev.db` project 20 的真名 `南宫爵` / `安小落` 全库查不到，KG 反而存了噪声 `{南宫, 安小, 房间, 终于}`

### 1.1 第一类漏检：`_valid_name` 对非百家姓姓氏 + 3 字名整体拒收

```python
# backend/app/kg/extractor.py:317
def _valid_name(w: str) -> bool:
    ...
    if w[0] in _SURNAME_CHARS:
        return True
    return all(c in _NAME_CHARS for c in w)  # ← 双门槛「姓首字 OR 全字名字用字」
```

实测：
- `_valid_name('南宫爵')` = **False**（`南` 在 `_SURNAME_CHARS` 缺，3 字名中 `宫`/`爵` 又不全在 `_NAME_CHARS`）
- `_valid_name('安小落')` = **True**（`安` 在 `_SURNAME_CHARS`），**但**因下条原因仍被截成 `安小`

### 1.2 第二类漏检：`_leading_name` 只取前 2 字，3 字名被截

```python
# backend/app/kg/extractor.py:174, 356
_LEAD_RE = re.compile(r"([一-鿿]{2,3})")  # 注释里说支持 2-3，但...
def _leading_name(sentence: str) -> str | None:
    s = sentence.strip().lstrip(_WRAP)
    m = _LEAD_RE.match(s)
    if not m: return None
    # ↑ _LEAD_RE.match 在 s 上只命中开头的连续 2-3 字符并返回 match
    #   后续 _valid_name 检查时若 2 字合法，函数就 2 字 return 了
    #   不会去试 3 字变体
    ...
    if _valid_name(tok):
        return tok
    ...
```

实测：`安小落咽了咽口水` → 抽到 `安小`（2 字合法，函数立即 return），`落` 字丢失。

### 1.3 第三类问题：句首主语噪声渗入

实测 `novel_dev.db` project 20 的 KG 节点 = `{南宫, 安小, 房间, 终于}`：
- `房间` = 句首分词「房间」被错认人名（`房`/`间` 都在 `_NAME_CHARS`）
- `终于` = 句首「终于」被错认（`终`/`于` 都在 `_NAME_CHARS`）

这俩**没有**触发 `_valid_name` 黑名单（因为它们 2 字、不在 stop 表里）。需要补 `_NAME_WORD_STOP`。

### 1.4 入图门槛：保留 `chapters >= 2` 不动

```python
# backend/app/kg/extractor.py:299
def _passes_entry_gate(c: Character) -> bool:
    return bool(c.named or c.speaking or (c.mentions >= 2 and len(c.chapters) >= 2))
```

`chapters >= 2` 分支在单段抽取**不可达**（`extract_from_text` 把所有 chunk 都标同一 `segment_no`）——但**别动**这条。这是已知设计，user 已认可；要修的是"named/speaking 走通"，不是改门槛。

---

## 2. 改动清单（最小、不动结构）

### 改动 1 · `_NAME_WORD_STOP` 加 6 条噪声

文件：`backend/app/kg/extractor.py`，第 72 行附近的 `_NAME_WORD_STOP = {` 块。

**当前已有但漏了的**，加：

```python
# E-任务：现代网文/口语高频词在 _NAME_CHARS 中全字命中，被误当人名。
# 这条不能依赖"全字在 _NAME_CHARS"放行，必须显式拒。
"房间", "终于", "剩下", "以前", "现在", "以后",
```

加完后**实测**：`房间`/`终于` 不再被 `_leading_name` 接收为候选名。

### 改动 2 · `_SURNAME_CHARS` 补现代网文高频姓

文件：同文件，第 30 行附近 `_SURNAME_CHARS = set(...)` 块（找到那一行）。

**加**：

```python
# E-任务：现代网文高频姓氏（不在百家姓但 90% 真实人名用）
"南", "宫", "墨", "凌", "宁", "楚", "霖", "翊", "琛", "瑜",
"萧", "顾", "沈", "韩", "秦", "温", "黎", "霍", "白", "蓝",
"容", "钟", "田", "江", "阮", "毕", "苗", "习", "乔", "伍",
# 复姓（务必补齐最常见的 18 个）
"慕容", "欧阳", "上官", "皇甫", "令狐", "尉迟", "诸葛", "司马",
"东方", "独孤", "夏侯", "尉迟", "宇文", "长孙", "慕容", "公孙",
"赫连", "拓拔", "呼延", "纳兰",
```

> **注意**：复姓在 `w[0] in _SURNAME_CHARS` 检查时，2 字 w 的首字是复姓第 1 字（如 `慕`）就算合法；但 `_valid_name` 还要求 `len(w) >= 2`，所以 4 字字符串才能完整过——大多数复姓是 3 字（慕容+名）也走的是 `_SURNAME_CHARS` 单字首字判定（慕容 → `慕` 命中），但**前提是 3 字 w 的首字必须在 `_SURNAME_CHARS`**。**所以上面的单字补就够**，复姓 4 字就 2 字姓 + 2 字名。

加完后**实测**：`_valid_name('南宫爵')` = **True**（`南` 命中 `_SURNAME_CHARS`），`_valid_name('安小落')` = True（`安` 已存在）。

### 改动 3 · `_leading_name` 先试 3 字、3 字失败回退 2 字

文件：第 356 行 `def _leading_name` 完整重写。

```python
def _leading_name(sentence: str) -> str | None:
    """取分句首 N 字作主语候选。
    
    E-任务：原实现只取 2 字（3 字名被截成 2 字丢失信息）。改为：
    1) 先试 3 字——3 字命中 _valid_name 即返 3 字；
    2) 3 字不命中，再剥尾虚字（又/便/就/也/而…）后试 3 字；
    3) 还不行再试 2 字（剥尾虚字同理）。
    这样 3 字名「安小落/林夜深/秦九歌」不丢，2 字名「林夜/陆见」也保。
    """
    s = sentence.strip().lstrip(_WRAP)
    if not s:
        return None
    
    # 收尾虚字集：与原实现保持一致，避免改变现有 2 字名行为
    _TAIL_VIRT = set("又便就也而但却仍只就即已曾忽急正先后尚复再方才")
    
    def _try_n(n: int) -> str | None:
        """取 s 前 n 字，剥尾虚字后试 _valid_name。"""
        if len(s) < n:
            return None
        tok = s[:n]
        # 一次剥尾虚字
        if tok and tok[-1] in _TAIL_VIRT:
            tok2 = tok[:-1]
            if len(tok2) >= 2 and _valid_name(tok2):
                return tok2
        if len(tok) >= 2 and _valid_name(tok):
            return tok
        return None
    
    # 先 3 字后 2 字（不要先 2 字后 3 字——那样会被 2 字前缀截胡）
    return _try_n(3) or _try_n(2)
```

> **关键纪律**：先 3 后 2。**不能**先 2 后 3——原注释（line 469-475）说"判据刻意用「前缀也是候选」这一群体证据"——如果先 2 字，2 字真名会截胡 3 字真名（`安小` 命中后函数立即 return，永远试不到 `安小落`）。

### 改动 4 · 不动的地方

- ❌ **不动** `_passes_entry_gate`（line 299）的 `chapters >= 2` 条件——已知设计，user 认可
- ❌ **不动** `merged_constraint_items` 签名（D 独占）
- ❌ **不动** `_SEG_RE` / `_SPEAK_RE` / `_CALLED_RE` / `_LEAD_RE` 正则本身
- ❌ **不动** `_GLUE_TAIL_CHARS` / `_DESC_STOP`（除非 fixture 真的 fail）
- ❌ **不动** `_write_result` / `merge_result_into_graph` / `get_graph`（F 独占）

---

## 3. 验收（用文本 fixture，**不查 db**）

### 3.1 新建 fixture + 测试

**新建** `backend/tests/__init__.py`（如已存在则跳过），`backend/tests/fixtures/extractor_v2_cases.json`：

```json
[
  {
    "name": "南宫爵+安小落（项目 20 风格）",
    "text": "“水？”南宫爵重复了一遍这两个字。\n\n安小落眨了眨眼，视线聚焦在他脸上。\n\n“要喝水，自己倒。”南宫爵又说了一遍。\n\n安小落咽了咽口水，喉咙干得发疼。",
    "expect_characters": ["南宫爵", "安小落"],
    "expect_no_noise": ["南宫", "安小", "房间", "终于"]
  },
  {
    "name": "林深+苏晚（再验 3 字名+句首主语）",
    "text": "林深坐在窗边，看着窗外的雨。苏晚走进房间，递给他一杯茶。\n\n终于，他开口了。",
    "expect_characters": ["林深", "苏晚"],
    "expect_no_noise": ["终于", "房间"]
  },
  {
    "name": "秦九歌+陆见（3 字名混 2 字名）",
    "text": "秦九歌沉吟片刻。陆见问道：“你想好了？”\n\n秦九歌点点头：“走。”",
    "expect_characters": ["秦九歌", "陆见"],
    "expect_no_noise": ["秦九"]
  },
  {
    "name": "复姓欧阳靖+普通李明",
    "text": "欧阳靖看向李明。“你来了。”\n\n李明点点头，没说话。",
    "expect_characters": ["欧阳靖", "李明"],
    "expect_no_noise": ["欧阳"]
  },
  {
    "name": "宝玉/黛玉/凤姐/贾母（红楼梦风格——这是 _valid_name 已通过的回归用例）",
    "text": "宝玉拉着黛玉的手。凤姐笑道：“老太太来了。”贾母点点头。",
    "expect_characters": ["宝玉", "黛玉", "凤姐", "贾母"],
    "expect_no_noise": ["玉拉", "姐笑"]
  },
  {
    "name": "叫 X 显式命名（_CALLED_RE 路径）",
    "text": "他有个朋友叫陆见。陆见说道：“你好。”",
    "expect_characters": ["陆见"],
    "expect_no_noise": ["见说"]
  },
  {
    "name": "X说/道/问（_SPEAK_RE 路径）",
    "text": "林夜说：“走吧。”秦九歌问道：“去哪？”",
    "expect_characters": ["林夜", "秦九歌"],
    "expect_no_noise": ["夜说"]
  },
  {
    "name": "粘连体（凤姐道 → 凤姐，不是 凤姐道）",
    "text": "凤姐道：“林之孝家的，可知昨日的事？”",
    "expect_characters": ["凤姐", "林之孝"],
    "expect_no_noise": ["凤姐道", "姐道"]
  }
]
```

**新建** `backend/test_extractor_v2.py`：

```python
"""E 任务验收：抽取器 3 字名 / 噪声护栏回归。

8 个 fixture 全部 PASS = 任务完成。失败回退：把 fail 的 fixture + 当前 3 字段
值/反例给 WorkBuddy 收口人，不要硬塞。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.kg.extractor import RuleBasedExtractor  # noqa: E402
from app.kg.build import extract_from_text       # noqa: E402
from app.kg.build import _TextChunk              # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "tests" / "fixtures" / "extractor_v2_cases.json"


def _to_chunks(text: str) -> list:
    # 走生产路径：extract_from_text 内部 _split_text_to_chunks 会把整段
    # 当一个 chunk，chapter_no 标 segment_no=1
    return extract_from_text(text, segment_no=1)[1]


def run() -> int:
    cases = json.loads(FIX.read_text(encoding="utf-8"))
    extractor = RuleBasedExtractor()
    fail = []
    for i, c in enumerate(cases):
        chunks = _to_chunks(c["text"])
        result = extractor.extract(chunks)
        chars = list(result.characters)
        miss = [n for n in c["expect_characters"] if n not in chars]
        noise = [n for n in c["expect_no_noise"] if n in chars]
        ok = (not miss) and (not noise)
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] case {i}: {c['name']}")
        if not ok:
            print(f"        抽到: {chars}")
            if miss:  print(f"        漏:   {miss}")
            if noise: print(f"        噪声: {noise}")
            fail.append(c["name"])
    print("=" * 60)
    if fail:
        print(f"FAIL  {len(fail)}/{len(cases)}: {fail}")
        return 1
    print(f"OK    {len(cases)}/{len(cases)} fixture 全过")
    return 0


if __name__ == "__main__":
    sys.exit(run())
```

### 3.2 跑测试

```bash
cd backend
python test_extractor_v2.py
# 期望：OK    8/8 fixture 全过
```

### 3.3 跑基线（防退化）

```bash
cd backend
python verify_sentinel.py 2>&1 | tail -3
# 期望：PASS 96  FAIL 0  ← G1b 转绿
python verify_acceptance.py 2>&1 | tail -3
# 期望：PASS 14  FAIL 0
python test_p0_regression.py 2>&1 | tail -3
# 期望：55/55
python test_ledger.py 2>&1 | tail -3
# 期望：40/40
python test_m3.py && python test_m3_relations.py && python test_m3_api.py
# 期望：全过
```

---

## 4. Worktree 流程

```bash
cd /c/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator

# 1) 从基线 HEAD 拉 worktree
git worktree add worktrees/fix-extractor-3char-and-noise 55a12a2

# 2) 切到 worktree 干活
cd worktrees/fix-extractor-3char-and-noise

# 3) 改 backend/app/kg/extractor.py（按 §2 改动 1+2+3）
#    改完后 git status 应只看到 1 个 M 文件

# 4) 新建 backend/tests/fixtures/extractor_v2_cases.json
#    新建 backend/test_extractor_v2.py

# 5) 跑 §3.2 + §3.3 全过

# 6) commit
cat > /tmp/msg_e.txt <<'EOF'
fix(extractor): 3 字名/姓氏/噪声护栏

E 任务（独立 BUG，从基线 55a12a2 拉 worktree）：
1) _NAME_WORD_STOP 加 6 条：房间/终于/剩下/以前/现在/以后
2) _SURNAME_CHARS 补 ~30 个现代网文高频姓 + 18 个复姓
3) _leading_name 改 3 字优先（先 3 后 2，避免 2 字前缀截胡 3 字名）

不动 _passes_entry_gate 的 chapters>=2 已知设计；
不动 _SEG_RE/_SPEAK_RE/_CALLED_RE/_LEAD_RE 正则；
不动 _GLUE_TAIL_CHARS/_DESC_STOP（除非 fixture fail）。

验收：tests/fixtures/extractor_v2_cases.json 8/8 PASS；
verify_sentinel 96/0 (G1b 转绿)；
verify_acceptance 14/0 / test_p0 55/55 / test_ledger 40/40 不退化。
EOF
git -c commit.gpgsign=false add backend/app/kg/extractor.py \
    backend/test_extractor_v2.py backend/tests/fixtures/extractor_v2_cases.json
git -c commit.gpgsign=false commit -F /tmp/msg_e.txt

# 7) 通知 WorkBuddy 收口（不用 push——本仓无 remote）
#    汇报：worktree 路径 + commit hash + 8/8 PASS 截图 + 总闸门 4 套输出尾
```

---

## 5. 失败回退

- 任何 fixture 失败 → **不继续改**。把：
  - fail 的 case 名
  - 当前 `_valid_name('南宫爵')` / `_valid_name('安小落')` / `_valid_name('房间')` 的 bool 返回
  - 当前 `_SURNAME_CHARS` 前 20 个元素
  - 当前 `_NAME_WORD_STOP` 前 10 个元素
  拍照给 WorkBuddy 收口人。
- `_passes_entry_gate` 改了 → 立刻 `git reset --hard HEAD` 回滚
- 误改其他文件（`extractor.py` 之外）→ `git restore` 那些文件
- 总闸门任一套件退化 → 立刻 `git reset --hard HEAD` + 报"extractor 修改导致基线退化"

---

## 6. 不要做

- ❌ 不要 push（无 remote）
- ❌ 不要改 `extractor.py` 之外的文件
- ❌ 不要扩 80 个名字用字（**只补 fixture 验证必需的**；过度扩会扩大误报，宁可少加）
- ❌ 不要把 `_valid_name` 的双门槛改成"或全字名字用字 OR 姓首字"以外的复杂规则
- ❌ 不要碰 `_passes_entry_gate`
- ❌ 不要在测试里读 `novel_dev.db`（验收必须 fixture-only）

---

**End of task E spec**
