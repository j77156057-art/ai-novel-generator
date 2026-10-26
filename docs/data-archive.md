# 参考语料与文风档案的长期存放

> 参考语料与文风档案是整个项目里**最贵的东西** —— 真书完整档蒸馏跑了几十小时，
> 烧掉的额度能再蒸馏几十遍。它们原来只以「实验库里切好的分片 + 蒸馏库行」
> 的形式存在，而 `scratch` 是**实验产物区**（被清过好几轮）。
>
> 现在导出成一份**自足**的归档，放在**仓库外**，并配了可复跑的核对工具。

## 1. 位置与理由

```text
$NOVEL_DATA_DIR/
  MANIFEST.json                          清单（指纹 + 书目 + 档案索引）
  corpus\雪中悍刀行\
    chunks.jsonl                         分片原文（逐字，可精确还原回 novel_chunks）
    novel.txt                            按章去重叠的阅读版（不是还原依据）
    meta.json                            书目信息、章数、片数、分片摘要
  style-archives\
    xuezhong-full-v7-ch1557.json         完整档 v7 · 1557 章采样 · 3508 条规则
    xuezhong-full-v7-ch1596.json         完整档 v7 · 1596 章采样 · 5061 条规则
    xuezhong-full-v7-ch297.json          完整档 v7 · 297 章采样  · 570 条规则
    xuezhong-full-v8-ch297.json          完整档 v8 · 297 章采样  · 3159 条规则
```

**为什么不放 git 仓库**：雪中悍刀行是**有版权的小说正文**，仓库里只放
路径、指纹与还原方法，不放内容。仓库外目录的另一个好处是它**不会**被
`git clean`、分支切换或代码评审顺手带走。

## 2. 里面是什么

| 项 | 值 |
|----|-----|
| 语料 | 雪中悍刀行（`xz.txt`）**990 章 / 5323 片 / 5,459,543 字** |
| 档案 | 蒸馏库里的 4 条 full 档叙事档案（15–17 个维度，570–5061 条规则） |
| 语料分片摘要 | `6226ea9a305f25f6f1c099a1503f57e8bfae646b9e295ed48567de1e2642373f` |
| `chunks.jsonl` | 15.9 MB，sha256 `769b69602dacb6b9…` |
| `novel.txt` | 15.4 MB，sha256 `7a27be3092c5d890…` |

**分片之间有 200 字重叠**（`chunk_overlap`）：`novel.txt` 是去重叠后的阅读版，
**还原必须用 `chunks.jsonl`**（逐字原样，含 `raw_text` 清洗前原文）。

## 3. 怎么核对（不用相信导出，跑一遍）

```powershell
python backend/verify_corpus_health.py   # 或按语料目录自定义核对
```

核对四件事：

1. 清单里每个文件的 sha256 与实际一致；
2. 语料分片的**逐字摘要**与清单一致；
3. 每份档案能解析，维度数 / 规则条数与清单一致；
4. **真把语料导入一个空库**再跑一遍语料抽检
   （`app.agents.audit.run_corpus_audit`）—— 证明它不只是字节对，
   而是切分/清洗/审核这条链能真的跑起来。

最近一次结果：

```text
语料 chunks.jsonl   OK 769b69602dacb6b9
语料 novel.txt      OK 7a27be3092c5d890
分片摘要 5323 条 OK 6226ea9a305f25f6
档案 xuezhong-full-v7-ch1557.json OK 15 维度 / 3508 条规则
档案 xuezhong-full-v7-ch1596.json OK 16 维度 / 5061 条规则
档案 xuezhong-full-v7-ch297.json  OK 16 维度 / 570 条规则
档案 xuezhong-full-v8-ch297.json  OK 17 维度 / 3159 条规则
真导入 5323 片 → 语料抽检 6 条 issue（error 0）
核对全部通过 ✓
```

## 4. 怎么用回去

**语料**：`chunks.jsonl` → 新建 `raw_novels` 一行 + 按 `chapter_no` / `idx`
插回 `novel_chunks`（**不要重新切分** —— 重新切分会得到不同的分片，
正文里的引文定位会全部失效）。核对工具第 4 步就是这个流程的可执行版本。

**档案**：`style-archives/*.json` 是**完整叙事档案 JSON**（`rules` + `forbidden`
+ `tier_dimensions` + `fingerprint` …）。灌回 `style_distills.style_narrative`
后就能在「蒸馏库」里被 `POST /api/projects/{id}/style-distills/apply` 应用到项目。

> 注意：蒸馏库这列在库里是**双重 JSON 编码**的（存的是 JSON 字符串而不是
> JSON 对象）。导出时已解开，灌回去时记得按原来的形态写
> （`json.dumps(narrative)` 存字符串），否则 `style_distills` 读出来会直接炸。

## 5. 原件还在哪

导出之后，实验库里的原件**仍然在**（多处副本）：

```text
<临时目录>/stage3/cross-relay/work-1/cross-relay.db
<临时目录>/stage3/realbook-audit-2/isolated.db
…（scratch 下共 156 个库文件，合计 19.2 GB）
```

它们是**实验产物**：可以随时被清。归档才是要长期留的那一份。

## 6. 导出脚本

导出用的是一次性脚本（跑完即弃），核对工具已入库（`tools/verify_data_archive.py`）。
重新导出时照着这两步做即可：**先导出并记录指纹 → 再跑核对工具**，
不要只导出就宣布「已备份」—— 导出件没验过就等于没备份。
