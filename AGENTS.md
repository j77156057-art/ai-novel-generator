# AGENTS.md

## 0. 开局先做这三件事

```powershell
git branch --show-current
git log -1 --format='%h %ci %s'
git status --porcelain
```

1. 当前分支必须是 `feat/resume-push-2026-10`。
2. HEAD 应当是 `869ed62`，或它的后代（`git merge-base --is-ancestor 869ed62 HEAD` 返回 0）。
3. 工作区里的 `M` / `??` 只要不是你要改的，就当作别的会话在途文件（见 §4）。

任何一条不满足：**停下，先向用户确认，不要动手修**。

## 1. 分支：只在一个地方干活

- 唯一可写分支：`feat/resume-push-2026-10`。
- 归档/只读：`master`（本地）、`origin/main`（远端）、标签 `freeze/main-cb05a45`、`freeze/feat-20261115`；`backup/*`、`codex/*`、其他 `feat/*` 一律不要动。
- **禁止**在归档或非权威分支上提交、修补、cherry-pick、rebase、合并，也不要「顺手修一下」。
- 发现自己在别的分支：先不要动工作区，确认未提交改动归属后问用户；有未提交改动就先停。

## 2. 测试与门禁：证据只认 XML

- 入口是仓库根 `pytest.ini`（`testpaths` 已写好），不要另拼一套收集范围。
- `backend/test_*.py`、`worktrees/*/test_*.py` 是自带 `sys.exit()` 的脚本，不进 pytest 收集，别算进「通过数」。
- 跑门禁必须落 XML（完整门禁约 30 分钟）：

```powershell
backend\venv\Scripts\python.exe -m pytest -p no:cacheprovider --junitxml="$env:TEMP\gate-<date>.xml"
```

- 报数只从 XML 读：

```powershell
([xml](Get-Content "$env:TEMP\gate-<date>.xml")).testsuite | Select-Object tests,failures,errors,skipped
```

- **不要**用终端最后一行、`$LASTEXITCODE`、或管道（`Tee-Object`）后的返回码当证据——管道会让退出码变成管道末端命令的。
- 结论必须带数字，例如「tests=2658 failures=0 errors=0 skipped=6」。没有 XML 数字就不许说「全过」。
- **门禁口径是 2658**（自 2026-11-17 `c91b794`/`7de0c34` 起，`testpaths = tests backend/tests`）。报数必须说「2658 口径」；拿 1969 对比会得出错误结论——差的不是回归，是收集范围（1969 → 2641 是 `testpaths` 扩收，2641 → 2658 是新增用例：`backend/tests` +15、`tests/` +2）。全量产物要说明覆盖到哪个 HEAD（最后一次全绿是 `c9606c7`，2658 口径 / 0 failed / 6 skipped）。
- 定向测试绿了不等于全量绿；批次收口必须跑一次完整门禁（教训见交接文档 §7.3 第 9 条）。

## 3. 不许做的事

- 不 `git push`（任何分支、任何情况，除非用户当次明确授权）。
- 不 `commit --amend`、不 `push --force`、不 `reset --hard`、不 `checkout --` 覆盖别人改动。
- 不伪造状态或数据让测试变绿；不为变绿放宽断言、加 `skip`、把 `xfail` 改成非严格。过期的 `strict xfail` 要删标记，不是放宽。
- 不动别的会话在途的 `M` / `??` 文件；不 stash、不格式化、不「顺手清理」。
- 不删、不改、不迁移只读库：`novel_dev.db`、`kg_vol1.db`、`kg_new.db`。新产物只写 `experiments/` 下的新目录。
- 不把路径写死到 `<temp>`；临时文件用 `tmp_path` / `tempfile`。
- **迁移有运行副作用**：`backend/app/models.py`、`backend/app/db.py` 一旦保存，8000 端口的守护进程在服务重启时会执行 `init_db()` / `_migrate_sqlite_columns()`，DDL 会直接落到生产库 `backend/novel_dev.db`。动这两个文件前，先停守护（顺序：先 uvicorn、后 venv shim），或先在副本上验证；做备份前先确认 8000 无监听，再复制/`VACUUM INTO`，再算 sha256。

## 4. 怎么判断「这是别人的文件」

`git status --porcelain` 里出现、但和本次任务无关的 `M` / `??`，一律视为别人的在途文件：

- 不改内容，不 `git add`，不提交，不删除。
- 需要它才能继续时：停下，把文件路径列给用户，等指示。
- 不要相信任何静态清单（分支、HEAD、未提交文件都会在几小时内变）。

## 5. 查不到就写「查不到」

- 找不到文件、提交、报告、数字、原始定义时，写「查不到」+ 你查过的路径/命令，不要用「应该是」「大概」「通常」补。
- 引用数字必须能指到文件、行号或实测输出（SQL / XML / 命令）；指不到就不写。
- 报告里区分「已验证」与「推断」，推断必须显式标注。

## 6. 先读哪份文档

按顺序：

1. `reports/2026-11-15-platform-gap.md`：平台自跑缺口的取证与基线数字。
2. 代码本身：结论冲突时以代码和测试为准。

**内部交接文档在仓库外，由维护者另给**（包括交接文档、模块级覆盖台账、批次对照表、
真实书目回放记录）。仓库里不再保留副本；需要时向维护者索取，不要按旧路径查找。

不要只读 README 就开工；README 不包含禁用分支、门禁口径和在途文件信息。
