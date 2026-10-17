# backend/tests — 脚本式断言工具（**不是** pytest 测试）

> 结论先讲：本目录下的 `test_*.py` **不是废测试，也不是 pytest 用例**，而是**脚本式断言工具**。
> 处置方向：**保留 + 文档定性 + 一键运行入口**。不要删除，也不要就地改写成 pytest 用例。

## 1. 这里有什么

| 文件 | 形态 | 直接跑的当前结果 |
|---|---|---|
| `test_finale_clean.py` | `main()` + `if __name__ == "__main__"` | 19/19 通过 |
| `test_style_exemplar.py` | 同上 | 22/22 通过 |
| `test_style_warning.py` | 同上 | 13/13 通过 |
| `test_name_normalize_probe.py` | **模块级** `sys.exit(...)`（约 `:180`） | 54 条断言全部通过（`sys.exit(0)`） |
| `test_narrative_extract.py` | `main()` + `if __name__ == "__main__"` | **33/34，1 条红**（见 §4） |
| `run_legacy_asserts.py` | 一键运行入口（本次新增） | — |
| `fixtures/`、`__init__.py` | 既有资产 | — |

## 2. 为什么它们不进 pytest 收集

- 这些文件里**没有任何 `test_*` 函数**，只有 `main()` 和模块级语句，
  所以即便被 pytest 扫到，也是 **收集 0 个用例**（`exit 5 / NO_TESTS_COLLECTED`）。
- 配置层面也收不到它们：`backend/pytest.ini` 里 `testpaths = ../tests`，
  只收集项目根的 `tests/`；根 `pytest.ini` 的注释里同样写明：
  > 老的 backend/test_*.py 是独立运行的 shell 脚本式断言，它们自己 `sys.exit()`，
  > **不**进 pytest 收集——别把它们当 pytest 测试。

**它们是脚本，不是用例。** 判定标准是**进程退出码**，不是 pytest 的 pass/fail。

## 3. 怎么跑

### 全部跑（推荐，一键入口）

```bash
# 从仓库根
backend/venv/Scripts/python.exe backend/tests/run_legacy_asserts.py

# 从 backend 目录（入口脚本用 __file__ 推导路径，两种 cwd 都可用）
venv/Scripts/python.exe tests/run_legacy_asserts.py
```

`run_legacy_asserts.py` 会：

1. 用门禁解释器 `backend/venv/Scripts/python.exe`（缺失时降级到当前解释器）逐个 `subprocess` 执行 5 个脚本；
2. 固定 `cwd = backend/` 且 `PYTHONPATH` 注入 `backend/`，**从任意 cwd 都能 import `app`**；
3. 汇总表打印每个脚本的通过数 / 失败数 / 退出码，并附末尾输出；
4. **任一脚本非零退出 → 整体退出码 1**（将来挂 CI 直接用退出码判定）。

### 单个跑

```bash
# 注意：请 cd 到 backend/ 再跑（见下方警告）
cd backend
venv/Scripts/python.exe tests/test_finale_clean.py
```

> ⚠️ **`test_name_normalize_probe.py` 依赖 cwd**：它写的是 `sys.path.insert(0, ".")`，
> 只有 cwd 为 `backend/` 时才能 import `app`。从仓库根直接 `python backend/tests/test_name_normalize_probe.py`
> 会 ModuleNotFoundError。其余 4 个用的是 `__file__` 推导的绝对路径，cwd 无关。
> 想省事就一律走上面的一键入口，它已经把 cwd 固定好了。

## 4. 已知状态：`test_narrative_extract.py` 有 1 条红

- 现象：33/34，失败项为 `FakeDB` 相关断言。
- 原因：脚本里的测试替身 `FakeDB`（`test_narrative_extract.py:136`）只实现了
  `commit()` / `rollback()`，**缺 `scalars()`**，而生产侧 `project_narrative_safe` 现在会调用 `db.scalars(...)`。
  属**测试替身过时**，**不是生产 bug**。
- 处置：**本次不修**。另立项修复，范围见 §6。
- 一键入口会**如实把这条红报出来**（exit=1，整体退出码 1）。**不要**为了汇总变绿去排除它或改判定。

## 5. 为什么**不**把它们迁进项目根 `tests/`

已经实测过，两个坑，别再试：

1. **迁移零增益**：原样搬进根 `tests/` 后，4 个文件仍然**收集到 0 个用例**（没有 `test_*` 函数），
   门禁覆盖率**零增益**，只是多了 4 个永远不被执行的死文件。
2. **会炸掉整场门禁**：`test_name_normalize_probe.py` 有**模块级 `sys.exit(...)`**（约 `:180`）。
   一旦被 pytest import（收集阶段就会 import），它在 import 期直接抛 `SystemExit`，
   导致 pytest **INTERNALERROR** —— 这是 session 级崩溃，**同一场里其余几百个用例全都跑不到**。
   换句话说：把它放进 `tests/` 不是"多一个红"，是"整条门禁挂掉"。

## 6. 将来若真要纳入门禁（独立立项，本次不做）

需要把它们改写成真正的 `test_*` 函数形态，至少包含：

1. **拆掉 `main()`**：把断言拆成 `def test_xxx():`，用 `assert` 表达；
2. **`sys.exit` 换成 assert**：特别是 `test_name_normalize_probe.py` 的模块级
   `sys.exit(1 if FAILS else 0)`（`:180`）必须删掉——这是 §5 里 INTERNALERROR 的根因；
3. **补 `FakeDB.scalars()`**：给 `test_narrative_extract.py` 的测试替身补上 `scalars()`，
   修掉 §4 那条红；
4. 顺带把 `sys.path.insert(0, ".")` 换成 `__file__` 推导的绝对路径，去掉 cwd 依赖。

这属于**独立立项**范围，不在本次"保留 + 定性"的处置内。

## 7. 维护约定

- 这 5 个脚本**保持原样**，本次未改一个字节；新增/修改的是 `run_legacy_asserts.py` 与本 README。
- 判定逻辑不许"美化"：有红就报红，不许加白名单、不许 `skip`、不许从列表里删文件名。
- 改动生产代码后想确认有没有打挂这些断言，跑一次一键入口即可，退出码就是结论。
