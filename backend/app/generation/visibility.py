"""Read-only reference visibility; absent or unverifiable provenance is excluded."""
import copy

#: 每类排除最多留几个**例子**。
#:
#: 诊断要能回答三件事：排除了多少、为什么排除、举个例子长什么样 ——
#: 但不该把整本书的每一段都列出来。
#:
#: 实测（真书 990 章 + 边界在第 100 章）：样例采样会扫过全书，被边界挡掉的
#: 候选有 **4903 段**，逐条记账 → 预检响应里 `visibility_diagnostics` 独占
#: **470 KB**（整份响应 535 KB，界面会被这份体积拖住）。
#: 归并成「一类一 + 几个例子」后，同样的信息只有几百字节。
EXCLUDED_EXAMPLE_LIMIT = 5


def _compact_source(source):
    """把一个来源压成**能定位**的几个字段。

    原来的记账方式把整个命中对象塞进去（含正文），一份诊断里能带上几十万字
    —— 诊断要的是「哪本书第几章」，不是正文。
    """
    out = {}
    for key in ('novel_id', 'chunk_id', 'chapter_no', 'idx'):
        if key in source:
            out[key] = source[key]
    return out


class VisibilityPolicy:
    def __init__(self, project, novel_ids, *, unbounded_paths: frozenset[str] = frozenset()):
        """`unbounded_paths`：这些创作路径下，参考书是**写法参考**而不是
        「要接着写的故事」，全书都是合法的风格材料。

        ## 为什么需要它

        可见性默认是 fail closed：没有显式原文边界（`reference_cutoffs`）就整本挡掉。
        这对**续写**是对的 —— 不能让模型看见这本书的后续章节。

        但对**原创**是错的：原创的参考书只是「学写法」，不存在「故事的未来」要防，
        整本挡掉的结果是风格样例通道**从来没通过过**（脱敏样例要靠它才有原文可脱敏），
        而模型拿不到原文就写不出参考书的句子长度（实测差 4~5 倍）。

        ## 边界在哪

        `unbounded_paths` 由调用方按**创作路径**给出，不从项目字段推断：
        路径是每次生成请求的语义（同一项目可以走不同路径），
        而 `reference_cutoffs` 是用户显式给的边界，两者是不同层的东西。

        显式边界**永远优先**：用户设了 cutoff，就按 cutoff 走（哪怕在 unbounded 路径里）。
        """
        self.novel_ids = set(novel_ids)
        self.unbounded_paths = frozenset(unbounded_paths)
        self.cutoffs = {}
        self.diagnostics = {'authority': getattr(project, 'story_authority', None) or 'generated_branch', 'cutoffs': [], 'warnings': [], 'excluded': []}
        for item in getattr(project, 'reference_cutoffs', None) or []:
            if isinstance(item, dict) and type(item.get('novel_id')) is int and type(item.get('chapter')) is int and item['chapter'] >= 1 and item['novel_id'] in self.novel_ids:
                self.cutoffs[item['novel_id']] = item['chapter']
        self.diagnostics['cutoffs'] = [{'novel_id': n, 'chapter': c} for n,c in sorted(self.cutoffs.items())]
        for nid in sorted(self.novel_ids - self.cutoffs.keys()):
            if self.unbounded:
                self.diagnostics['warnings'].append(
                    f'novel_id={nid}: 写法参考路径，全书可见（无故事边界要防）')
            else:
                self.diagnostics['warnings'].append(f'novel_id={nid}: 待定位，未设置显式原文边界')

    @property
    def unbounded(self) -> bool:
        """没有显式边界的语料是否整本可见（仅限写法参考路径）。"""
        return bool(self.unbounded_paths)

    def reason(self, novel_id, chapter):
        if type(novel_id) is not int or novel_id not in self.novel_ids:
            return 'missing_or_unselected_novel'
        if novel_id not in self.cutoffs:
            if self.unbounded:
                # 写法参考路径：整本可见，但章节号仍要是真的（否则是坏数据）。
                if type(chapter) is not int or chapter < 1:
                    return 'missing_source_chapter'
                return None
            return 'missing_cutoff'
        if type(chapter) is not int or chapter < 1:
            return 'missing_source_chapter'
        if chapter > self.cutoffs[novel_id]:
            return 'future_source'
        return None

    def exclude(self, kind, reason, **fields):
        """记一条「被排除」——按 (kind, reason) 归并计数，只留有限个例子。

        为什么不逐条记账：见 `EXCLUDED_EXAMPLE_LIMIT` 的说明（真书一次 4903 条，
        预检响应被撑到 470 KB）。归并后诊断仍然是**可核对**的：总数在 `count`，
        代表例子在 `examples`，逐条明细要看就去查语料本身。
        """
        record = None
        for item in self.diagnostics['excluded']:
            if item.get('kind') == kind and item.get('reason') == reason:
                record = item
                break
        if record is None:
            record = {'kind': kind, 'reason': reason, 'count': 0, 'examples': []}
            self.diagnostics['excluded'].append(record)
        record['count'] += 1
        if len(record['examples']) < EXCLUDED_EXAMPLE_LIMIT:
            record['examples'].append(fields)

    def filter_hits(self, hits, kind):
        kept = []
        for hit in hits:
            if hit.get('source') == 'generated':
                kept.append(hit)
                continue
            sources = hit.get('sources') or [hit]
            reason = next((r for src in sources if (r := self.reason(src.get('novel_id'), src.get('chapter_no')))), None)
            if reason:
                self.exclude(kind, reason, sources=[_compact_source(s) for s in sources])
            else:
                kept.append(hit)
        return kept

    def narrative(self, narrative):
        if not isinstance(narrative, dict):
            return narrative
        result = copy.deepcopy(narrative)
        entries = []
        for entry in result.get('characters') or []:
            if not isinstance(entry, dict):
                continue
            chapters = entry.get('source_chapters')
            reason = 'missing_source_chapters' if not isinstance(chapters, list) or not chapters else next((r for ch in chapters if (r := self.reason(entry.get('novel_id'), ch))), None)
            if reason:
                self.exclude('l2', reason, name=entry.get('name'), novel_id=entry.get('novel_id'), source_chapters=chapters)
                self.diagnostics['warnings'].append(f"L2 {entry.get('name')}: {reason}")
            else:
                entries.append(entry)
        result['characters'] = entries
        return result
