"""知识图谱抽取：规则基线 + 可插拔接口（LLM 抽取后置）。

规则基线为占位质量，确定性、可离线验证，足以跑通 M3 整链：
- 角色：仅用高精信号——① 显式命名（叫X）② 分句主语（句首 / 逗号后 2 字 CJK）③ 说话归属（X说/道/问）。
        三条信号统一注册为候选角色；满足「命名种子 或 说话归属 或（作为主语出现≥2次 且 跨≥2章）」
        才入图。完全不用模糊滑窗，避免把常见词（发光/女生/是什么/条街）误判为人名。
        入图后再按精确名回扫全部分片，补全省略指代带来的章节归属（如「她给林夜」）。
- 坑：必须是「读者想知道但还不知道」的信息缺口。悬念关键词只作候选信号，
        命中后还要过句法形态闸门（见 _is_real_plothole）：带问号或疑问/未完成
        标记才放行；旁白预告式（"直到很多章以后，他才明白…"）、叙述者直接给答案
        （真相是/原来/这就是…）、以及「不会被遗忘。」这类金句式陈述全部拦掉。
        —— 早期版本只按关键词登记，把金句当坑，注入提示词后模型靠复述金句来
        "回收"，是生成段落AI 腔的直接来源。
        仅在「末现章之后」的章节出现解决线索才置 resolved，避免同章自解析。
- 关系：同分片共现的两角色 + 关系线索词 → 关系边。

正式质量（人物/坑/关系的高精度抽取）交给 LLM 抽取实现同一接口后置。
"""

import re
from dataclasses import dataclass, field

# 角色候选停用/功能词（作主语或说话者时直接排除）
_CHAR_STOP = set(
    "的了他她它我们你们他们她们自己这个那个一个没有什么怎么因为所以但是"
    "如果说已经还是不是就是可以知道看见觉得发现选择时候起来出去一下哪里谁哪"
    "没有这么那样只是本来其实于是突然渐渐慢慢要把被让对跟和与我你而也又再还"
    "更最很太能会想被把给叫".split()
)
# 常见姓氏（单姓 + 复姓首字）。用作「像不像人名」的**正向**信号：
# 原实现只靠 _DESC_STOP 黑名单，拦不住文言虚词（故/撰/忽/一说…），
# 实测红楼梦 27621 个分句里 22924 个（83%）被当成主语候选，
# 候选字符膨胀到4000+ 个，既拖慢抽取又让入图门槛形同虚设。
_SURNAME_CHARS = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚程"
    "嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧"
    "隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙叶幸"
    "司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双闻莘"
    "党翟谭贡劳逄姬申扶堵冉宰郦雍璩桑桂濮牛寿通边扈燕冀郏浦尚农温别庄"
    # E-任务：现代网文高频姓氏（不在百家姓但 90% 真实人名用），单字加进去即可被
    # _valid_name 第 351 行首字匹配命中。
    "南墨楚霖翊琛瑜霍"
    # 复姓首字：复姓是 2 字姓氏（慕容/欧阳/上官/皇甫/令狐/尉迟/诸葛/司马/独孤/夏侯/宇文/长孙/公孙/赫连/拓拔/呼延/纳兰），
    # 3 字人名 = 2 字姓 + 1 字名（如「慕容晓」），_valid_name 走「姓首字」分支
    # 只需把 2 字姓的**首字**进表（_valid_name 第 351 行只检查 w[0]）。
    # 已存在的复姓首字（慕/欧/司/公/东...）不算；只加 12 个未入库的：
    "皇令尉诸独夏宇长赫拓呼纳上"
    "晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文"
    "寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾毋沙"
    "乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)
# 名字常用字（名，不含姓氏字）。用于第二字/第三字的形态校验。
# 纪律：这里只放**能出现在人名里**的字，绝不放「不知/不能/东西/都是」这类
# 常用词——早期版本混入常用词，导致这些词走「全字在名字用字集内」的兜底分支
# 被判成人名（红楼梦实测泄漏出「不知/不能/东西/都是」角色）。
_NAME_CHARS = set(
    "伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超霞平刚桂辉健俊峰旭东阳西"
    "星辰云海山川林森江河湖岩松柏竹梅兰菊荷莲明清元宗文武平安康宁瑞"
    "祥福禄寿喜盈嘉惠青梅竹马青白鹿南枝观棋听雨望月怀瑾握瑜安宁敦"
    "彼柔静姝然施琅琚皎洁玉树临风清欢锦瑟深流岫悠霜霄汉宫阙光霁"
    "言知处澹台赵端木孤南西门东"
    # 经典小说高频名用字（宝玉/黛玉/凤姐/妙玉…），缺了会把真实角色整片拦掉
    "宝玉黛凤妙琴钗黛绛珠仙蟾钗宝钗湘云探春惜春迎春妙玉鸳鸯袭人"
    "平儿紫鹃雪雁莺儿金钏玉钏司棋侍书入画丰儿小红麝月秋纹碧痕晴雯"
    "宝玉钗黛凤姐贾母王夫人邢夫人李纨秦可卿尤氏李纹李绮王熙凤巧姐"
    # E-任务：现代网文/玄幻高频单名用字，3 字名「姓+单名」结构需要末字在表
    # 补「道」：常见名末字（王守道/张道陵/李道玄），不加则 3 字名末字校验
    # 会把这些真名误拒（实测 test_p0_regression「王守道」被拒）
    "落歌靖晓爵衍墨渊晚歆璃楚霖翊琛瑜凌道"
)
# 姓氏字撞上常用词的情况：百家姓里「东方」「都」是真姓，但「东西」「都是」
# 不是。实测这两个词因首字在姓氏表内而被判成人名。
# 用显式元组而非空格分隔长串——长串里词条会粘连/漏切（曾导致护栏失效）。
_SURNAME_WORD_STOP = {
    "东西", "都是", "不是", "东北", "东方", "都市", "部落", "郡县",
    "行程", "全部", "都说", "都可", "赶到", "躲过", "遇到", "部位",
}
# 兜底「全字在 _NAME_CHARS」分支的必要护栏。
_NAME_WORD_STOP = {
    "不知", "不能", "东西", "都是", "可以", "就是", "什么", "没有",
    "这样", "那样", "于是", "然后", "现在", "已经", "开始", "觉得",
    "知道", "看见", "发现", "出来", "进去", "起来", "下去", "回来",
    "意思", "事情", "时候", "地方", "声音", "眼神", "心里", "手里",
    "身上", "眼前", "身后", "里面", "外面", "中间", "上面", "下面",
    # 虚词 + 动词/形容词的组合（「都笑」「都说」「又惊又喜」…）：
    # 实测会造出「宝玉—都笑」这类伪关系。
    "都笑", "都说", "都问", "都看", "都听", "又惊又喜", "又笑又哭",
    "不说", "不听", "不看", "不理", "不怕", "不想", "不吃",
    # E-任务：现代网文/口语高频词在 _NAME_CHARS 中全字命中，被误当人名。
    # 这条不能依赖"全字在 _NAME_CHARS"放行，必须显式拒。
    # 实测漏：项目 20 的 KG 节点含「房间/终于」(project 20 段1句首"房间里终于..."等)
    "房间", "终于", "剩下", "以前", "以后",
}
_DESC_STOP = set(
    "女生男生孩子老人男人女人先生小姐姑娘少爷夫人大人家伙东西事情地方"
    "声音眼神手指巷口胸口霓虹视网膜警报灯管选择天亮本能地下代价名字"
    "清道夫接线人三层街角门口窗外夜色脸色手心背影脚步空气光芒寒意"
    "世界城市国家军队帝国门派宗门宫殿城堡岛屿海洋山谷森林平原"
    "活人死人等人人们活物问题原因结果情况部分方面过程阶段状态程度"
    "水平标准方式形式内容意义价值作用影响关系条件机会现实理想目标计划"
    "方案措施行动行为现象本质特征特点性质规律原则系统机制结构模式框架"
    "体系领域范围环境背景局面形势趋势方向道路途径渠道资源要素基础核心"
    "关键重点难点热点焦点亮点起点终点节点拐点转折点里程碑标志象征代表"
    "体现反映表明显示说明证明揭示展示呈现表现表达传达传递传播推广普及"
    "应用使用利用发挥实现完成达到取得获得形成建立构建创造产生出现发生"
    "发展变化改进改善提高提升增强加强扩大缩小增加减少降低控制管理处理"
    "解决应对面对迎接挑战风险危机困难障碍矛盾冲突合作竞争交流沟通协调"
    "配合支持帮助服务保障维护保护促进推动引导指导领导组织实施开展进行"
    "执行运行操作工作学习研究探索调查分析评估评价测试实验验证检查审查"
    "监督监管规范法律时间岁月年月日天年季季节天气气候温度颜色形状大小"
    "数量质量重量长度宽度高度速度距离面积体积密度强度硬度亮度".split()
)
# 分句连接/转折功能词（作分句主语时排除，避免误判为人名）
_FUNC_STOP = set(
    "或者然后于是但是因为所以如果虽然而且并且原来其实果然总之例如其中"
    "此外另外此时此刻如今后来从前刚才现在这里那里直到渐渐突然慢慢终于"
    "接着不过否则哪怕即使尽管只要只有无论不论与其不如反而甚至尤其特别"
    "比如换句话说也就是说虽然但而因所如若若那么这么那样虽说虽说虽说".split()
)

# 显式命名：叫X（X，是 / X是 过松，已弃用以避免「条街是」之类误抽）
_CALLED_RE = re.compile(r"叫([一-鿿]{2,3})")
# 说话归属：紧贴说话动词的 2–3 字真名，且左侧必须是非 CJK 边界。
# 左边界约束用于排除「那道凉意」「清道夫」把 道 误当说话动词、以及「林夜终于问」把 夜终于 误抓。
# 名字部分必须用**非贪婪** {2,3}?，并让动词短语覆盖三类：
#   ① 「X道」家族（说道/问道/笑哭怒嗔+道……用 [一-鿿]道 泛化，实测占对话归因绝大多数）
#   ② 「听说」（听觉转述，「宝玉听说」贪婪版会粘成伪角色「宝玉听」388 次）
#   ③ 单字（说/道/问/答/笑）
# 名与动词之间允许 0–2 个副词/虚字（「贾母又问道」「凤姐便道」）——不留槽位会把副词
# 粘进名字（实测伪角色「贾母又」78 次、「贾母便」102 次、「凤姐便」50 次）。
# 贪婪版还把「陆见说道」解析成 名字=「陆见说」+ 动词=「道」；非贪婪后先试
# 「2 字名 + 动词短语」，3 字名（「秦九歌说道」）自动回退到 3 字名匹配。
# 已知低频边界：3 字名末字恰为动词短语首字（「王守道说道」）会误判成 2 字名，接受。
_SPEAK_VERB_PATTERN = r"(?:听说|[一-鿿]道|道|说|问|答|笑)"
_SPEAK_ADV_PATTERN = (
    r"(?:又|便|因|都|也|却|竟|忙|遂|乃|复|再|方|才|只|就|即|已|曾|忽|急|正|先|后|仍|尚)"
)
_SPEAK_RE = re.compile(
    rf"(?<![一-鿿])([一-鿿]{{2,3}}?){_SPEAK_ADV_PATTERN}{{0,2}}{_SPEAK_VERB_PATTERN}"
)
# 说话动词字符集：供粘连检测复用（名末字与下一字都是它时，末字实为动词首字）。
_SPEAK_VERBS = "说道问答笑"
# 宾语位粘连体首字集合 = 说话动词 + 「听」（「听说」是转述动词短语，
# 「叫宝玉听说」的宾语位会切出「宝玉听」——只列说话动词会漏掉这一类）。
_GLUE_VERB_LEADS = set(_SPEAK_VERBS) | {"听"}
# 粘连尾巴集合：名末字命中且前缀本身是已入图角色时，末字应判为粘连（对话桥接纠偏用）。
_GLUE_TAIL_CHARS = set("说道问答笑听") | set("又便因都也却竟忙遂乃复再方才只就即已曾忽急正先后仍尚")
# 换行续行归一：源文本（公版书扫描档）按行硬折行，人名会被换行切成两半
# （实测「林黛⏎玉」17 次，占「林黛」全部出现的 5%；断点后的「玉冷笑道」还会被
# _SPEAK_RE 归因成新名字「玉冷」）。折行只可能在**前后都是汉字**时出现；
# 段末（前一字是标点，如「。”⏎」）绝不算续行——否则会把两段话缝在一起造假匹配。
_WRAP_NL_RE = re.compile(r"(?<=[一-鿿])[ \t]*\r?\n[ \t]*(?=[一-鿿])")


def _unwrap_lines(text: str) -> str:
    """去掉「汉字之间的换行」，还原被硬折行切断的词。

    **只用于覆盖度判定，不用作抽取输入。** 实测把它当全局输入归一反而更差：
    消歧后角色 1120 → 1122，但消失 48 个的同时**新造 50 个** 3 字切片
    （「凤姐⏎半夜」接起来后被宾语位兜底切成「凤姐半」、「平儿⏎走进」→「平儿走」），
    净变化是噪声互换而非改善。故此处只在「判断短名是否被长名完全覆盖」时借用它。
    """
    return _WRAP_NL_RE.sub("", text)


# 分句切分：保留终止符（含 ？），使「疑问符 + 悬念词」落在同一片段，避免问号被切走后漏判直接疑问。
# 注意终止符必须同时含**全角 ？**（U+FF1F）：早期版本只写了半角 ? 与全角 ！，
# 全角问号从未被当成分句边界，「？”\n下一句」被整段吞进同一片段，坑 question 实测
# 被吞成「究竟是谁干的？”\n白光沉默不语。」并注入提示词。句末同时**吃掉配对右引号**（“…” 的 ”）。
_SEG_RE = re.compile(r"[^。！？!?；;，,]+[。！？!?；;，,]?[”\"'’]?")
# 宾语位人名首字黑名单：宾语位兜底会切到「方才你」「应著」「都笑说」
# 「秦钟见」「贾母笑」这类「副词/动词 + 字」组合——它们首字在姓氏表内
# （百家姓确有「方/都/应/贾/秦」等），但真实人名不会以这些字开头成词。
_NAME_LEAD_STOP = set(
    "才方都应就还又再但却则使令把将为以于向从到跟同和便即另各每某"
    "很太最更再又才常每另各某极颇稍略挺蛮颇"
)
# 宾语位触发词：其后紧邻的 2–3 字词若是人名，即为宾语（「拉住苏晚」「问宝玉」）
_OBJ_TRIGGER_RE = re.compile(
    r"(?:拉住|握住|抓住|扶住|抱住|推开|递给|塞给|叫|喊|唤|问|答|帮|救|"
    r"打|杀|笑|看|望|等|遇|见|问起|告诉|求)"
    r"(?=[一-鿿]{2,4})"
)
# 句首 2 字 CJK 主语候选
_LEAD_RE = re.compile(r"([一-鿿]{2,3})")
# 单字虚词（切分主语时会黏在名字尾部，需剥除）
_TAIL_PARTICLE = set("又便就也而则遂乃复还且或但却则使令把将")
# 需要剥除的包裹符号（引号/括号/冒号/破折号等）
_WRAP = """"'"'"「」『』“”‘’()（）:：—-；;，,。！？!?"""

# 坑线索关键词
_PITHOLE_CUES = ["什么", "为什么", "究竟", "秘密", "谜", "真相", "代价", "遗忘", "怎么", "为何"]

# —— 坑必须是「信息缺口」，不能是已经说完了的陈述句 ——
# 规则基线的坑判定原先只要「命中关键词 or 带问号」就登记，导致
# 「不会被遗忘。」「本身就是最贵的代价。」这类**金句式陈述**被当成未填坑，
# 再注入生成提示词，模型只能靠复述它们来"回收"，于是产出 AI 腔。
# 下面按「句法形态」把三类假坑挡掉，只留真正的问句与未完成信息缺口。
# ① 已完成的断言：有主语 + 谓语陈述，且不含疑问/未完成标记
_ASSERT_COMPLETE_RE = re.compile(r"^(?:那|这|它|他|她)?[^。！？\n]{1,20}?[就是是乃为](?:最|不|也|都)?[^。！？\n]{0,24}[。！]?$")
# ② 旁白预告式（"直到很多章以后，他才明白……"）——作者已把答案说完，不是坑
_NARRATOR_RE = re.compile(r"^(?:直到|后来|很多年|多年|很多章|许多年|若干|某天|从此|而今|多年以后)")
# ③ 叙述者直接给答案的收尾句
_ANSWERED_RE = re.compile(r"(?:才明白|终于明白|原来[是为]|真相是|这就是|答案是|他说的是|所谓)")
# ④ 纯金句：短句 + 含评价性词汇 + 无信息缺口动词
_MAXI_RE = re.compile(r"(?:最|极|无比|真正|唯一|本质|意义|代价|宿命|深渊|救赎)")

# 疑问 / 未完成标记（命中即视为真信息缺口）
_QUESTION_MARK_RE = re.compile(r"[？?]")
_UNFINISHED_RE = re.compile(
    r"(?:吗|呢|吧|么)$"                # 句末语气助词
    r"|(?:如果|假如|要是|除非|是否|怎么|为什么|为何|究竟|到底)"  # 疑问词
    r"|(?:是不是|会不会|有没有|能不能|可不可以|该不该)"          # A-or-B 疑问固定式
)
# 坑解决线索（仅强短语；弱词 明白/原来/真相 噪声过大，误判率高，留给 LLM 抽取处理）
_RESOLVE_CUES = [
    "真相是", "原来是", "终于明白", "解密", "揭晓", "答案是",
    "谜底", "真相大白", "水落石出",
]

# 关系线索词（必须是人际交互动作，而非「和/看」这类中性动词的高频误配）。
# 「共现」不是关系词，它原本是 Pass 4 找不到线索时的兜底标签——既然现在
# 改为有向交互抽取，没有证据就不建边，不需要这个兜底。
_REL_CUES = ["说", "问", "答", "骂", "笑", "喊", "唤", "拉住", "握住", "推开",
             "递给", "塞给", "抓住", "扶住", "抱住", "打", "杀", "救", "帮",
             "问起", "告诉", "求"]
_REL_LABEL = {
    "说": "对话", "问": "对话", "答": "对话", "问起": "对话", "告诉": "告知",
    "骂": "冲突", "喊": "对话", "唤": "对话", "笑": "互动", "求": "求助",
    "拉住": "接触", "握住": "接触", "抓住": "接触", "扶住": "相助",
    "抱住": "亲密", "推开": "冲突", "打": "冲突", "杀": "敌对",
    "递给": "给予", "塞给": "给予", "救": "救命", "帮": "相助",
}

# 关系共现窗口：两角色名之间距离超过该值则不建边。
# 真实交互中「X 拉住 Y」「X 问 Y」的角色名间距通常在 20 字内；
# 放宽会把同一段里无关人物也连上，退回笛卡尔积。
REL_WINDOW = 20
# 冒号 + 引语开头：独白式引语的归因信号（「：“」/「："」/「：「」）。
# 用于「相邻对话桥接」——句内只有一个人名时，冒号引语即说话证据。
_SPEECH_QUOTE_RE = re.compile(r"[：:][\"“「『']")


def _is_real_plothole(sent: str, has_q: bool, unfinished: bool) -> bool:
    """句法形态闸门：判断一句话是否真的是「未填坑（信息缺口）」。

    背景：M3 规则基线原先只要「命中悬念关键词 or 带问号」就登记坑，于是
    「不会被遗忘。」「本身就是最贵的代价。」这类金句式陈述被当成未填坑，
    注入生成提示词后模型只能靠复述它们来"回收"，直接产出 AI 腔（金句台词）。

    规则（保守优先，宁可漏坑不可造坑）：
    1. 带问号 → 直接放行（读者确实想知道答案）；
    2. 疑问词结尾/未完成标记（吗/呢/如果/是否…）→ 放行；
    3. 旁白预告式（"直到很多章以后，他才明白…"）→ 拦（作者已把答案说完）；
    4. 叙述者直接给答案（真相是/原来/这就是…）→ 拦；
    5. 剩下的纯陈述句 → 拦（关键词命中不等于信息缺口）。
    """
    body = sent.strip().strip(_WRAP)
    if not body:
        return False
    if has_q:
        return True
    if _NARRATOR_RE.match(body):
        return False
    if _ANSWERED_RE.search(body):
        return False
    if unfinished:
        return True
    # 关键词命中但形态是完整陈述（且不是未完成句）→ 判定为金句/叙述，不算坑
    if _ASSERT_COMPLETE_RE.match(body) or _MAXI_RE.search(body):
        return False
    # 兜底：句中没有疑问形态也没有 cue 类词（由调用方保证有 cue），一律不算坑
    return False


@dataclass
class Character:
    name: str
    mentions: int = 0
    chapters: set = field(default_factory=set)
    speaking: bool = False
    named: bool = False


@dataclass
class Plothole:
    key: str
    cue: str
    question: str = ""
    first_chapter: int | None = None
    last_chapter: int | None = None
    status: str = "open"


@dataclass
class Relationship:
    a: str
    b: str
    relation: str
    chapters: set = field(default_factory=set)


@dataclass
class ExtractionResult:
    characters: dict = field(default_factory=dict)   # name -> Character
    plotholes: dict = field(default_factory=dict)    # key -> Plothole
    relationships: dict = field(default_factory=dict)  # (a, b) -> Relationship


def _passes_entry_gate(c: Character) -> bool:
    """Pass 2 入图资格——抽出来做**单一事实来源**，避免判据在多处漂移。

    三条通路任一成立即入图：
      named     经「叫X」显式命名获得；
      speaking  有说话归属证据；
      mentions≥2 且跨≥2 章 —— 多章复现是「不是一次性噪声」的最低证据。
    注意这条门槛**只用于入图**，不要拿它去约束 Pass 1.7 的覆盖者选取：
    实测那样做是净负面（详见 _drop_shadowed_prefixes 的实测决策段）。

    E-任务（已试加第四通路「句首主语≥2 即可入图」）实测净负面：让 P0
    退化 2 条（王守道被截、朱木兰误入图），verify_sentinel 仅净增 9 条 PASS；
    故回滚到原 3 通路设计。
    G1b 红得对的修复路径在**别处**——让 named/speaking 通路把角色"养"进图。
    """
    return bool(c.named or c.speaking or (c.mentions >= 2 and len(c.chapters) >= 2))


class KGExtractor:
    def extract(self, chunks: list) -> ExtractionResult:  # pragma: no cover - 接口
        raise NotImplementedError


def _valid_name(w: str) -> bool:
    """人名合法性：黑名单 + **正向形态**双重把关。

    正向形态是必须的：单靠黑名单时，文言虚词（故/撰/忽/一说/因曾…）几乎
    全被放行，候选膨胀到数千个。

    但正向表不能收得太紧：中文人名大量「非姓命名」（宝玉/黛玉/凤姐/贾母），
    只认姓氏会把红楼梦这类真实角色整片拦掉。因此规则是
    「姓首字」或「名字用字组合」皆可，但命中后仍须过 _NAME_WORD_STOP 护栏，
    否则「不知/不能/东西/都是」这类常用词会被当成名字。

    长度必须 ≥2：中文人名极少单字，而「人」「可」「然」「小」这类单字
    恰好在姓氏表内（百家姓确有「人」「可」等），放行会造出
    「人—宝玉」这种伪关系（实测该伪边跨 50 章）。
    """
    if len(w) < 2 or len(w) > 4:
        return False
    # 名字里不能有标点/句末符号：宾语位兜底会取到「拉住苏晚。」整段，
    # 把句号吃进名字（实测造出「苏晚。」这种伪角色）。
    if not all("一" <= c <= "鿿" for c in w):
        return False
    if w in _CHAR_STOP or w in _DESC_STOP or w in _FUNC_STOP:
        return False
    if w in _NAME_WORD_STOP or w in _SURNAME_WORD_STOP:
        return False
    if w[0] in "那这哪每各该":
        return False
    if w[0] in _NAME_LEAD_STOP:
        return False
    if any(c in "的一了着过吗呢吧啊哦呀呐" for c in w):
        return False
    if len(set(w)) != len(w):
        return False
    # 正向：首字是常见姓氏，或每个字都在人名用字集合内
    if w[0] in _SURNAME_CHARS:
        # E-任务：3 字名「姓+2字」的兜底过宽，会把「林深坐/林夜说/凤姐笑/房间里」错认。
        # 3 字名要求末字必须是名字用字（人名的「名」常用字），不允许落在普通动词/方位/代词上。
        # 2 字名「姓+1字」不走此约束（首字是姓 + 1 字本来就要灵活，1 字名空间小，过严会漏）。
        if len(w) == 3 and w[-1] not in _NAME_CHARS:
            return False
        return True
    return all(c in _NAME_CHARS for c in w)


def _leading_name(sentence: str) -> str | None:
    """取分句首 N 字作主语候选；尾部虚字（又/便/就/也/而…）需剥除。

    E-任务：原实现只取 2 字（3 字名被截成 2 字丢失信息）。改为：
    1) 先试 3 字——3 字命中 _valid_name 即返 3 字；
    2) 3 字不命中，再剥尾虚字（又/便/就/也/而…）后试 3 字；
    3) 还不行再试 2 字（剥尾虚字同理）。
    这样 3 字名「安小落/林深/秦九歌」不丢，2 字名「林夜/陆见」也保。

    「宝玉又拉住黛玉的手」直接取前 2 字会得到「宝玉」是对的，
    但「林夜便说」会取到「林夜」之外还可能切进虚词（「宝玉又」），
    故按需剥离尾部的单字虚词后再取候选。
    """
    # 分句正则会保留配对引号；引号后可能还有源文本硬折行/空白，
    # 需在剥 wrapper 后再清一次空白，否则「”\n秦九歌点头」无法识别句首人名。
    s = sentence.strip().lstrip(_WRAP).lstrip()
    m = _LEAD_RE.match(s)
    if not m:
        return None
    raw = m.group(1)  # 2-3 字（_LEAD_RE 模式 [一-鿿]{2,3}）
    # 关键纪律：先 3 后 2。**不能**先 2 后 3——否则 2 字真名会截胡 3 字真名
    # （「安小」命中后函数立即 return，永远试不到「安小落」）。
    for n in (3, 2):
        if len(raw) < n:
            continue
        tok = raw[:n]
        if _valid_name(tok):
            return tok
        # 退一步：剥掉尾部单字虚字再试（「宝玉又」→「宝玉」）
        if len(tok) >= 2 and tok[-1] in _TAIL_PARTICLE:
            alt = tok[:-1]
            if len(alt) >= 2 and _valid_name(alt):
                return alt
    return None


class RuleBasedExtractor(KGExtractor):
    def extract(self, chunks: list) -> ExtractionResult:
        res = ExtractionResult()

        # —— Pass 1：高精信号统一注册候选角色 + 坑线索 ——
        for ch in chunks:
            ch_no = ch.chapter_no or 0
            text = ch.text or ""

            # ① 显式命名（叫X）
            for m in _CALLED_RE.finditer(text):
                name = m.group(1)
                # 粘连防护：「叫陆见说道」会被贪婪吃成「陆见说」——named=True 是直接
                # 入图通行证，伪角色立刻进图谱。判据：被剥字的**首字 + 后一字**必须构成
                # 完整动词短语（陆见说|道 → 说道 ✓ 剥成 陆见；王守道|说 → 道说 ✗ 保留，
                # 避免把 3 字名误伤成 2 字）。「听说」同理（叫宝玉听说 → 宝玉听|说 → 听说 ✓ 剥）。
                # 末字集合必须含「听」（_GLUE_VERB_LEADS 而非 _SPEAK_VERBS）：
                # 「听」不在说话动词「说道问答笑」里，只查后者会让「宝玉听」照旧入图。
                if len(name) == 3 and name[-1] in _GLUE_VERB_LEADS:
                    pair = name[2] + text[m.end(): m.end() + 1]
                    if pair in ("说道", "问道", "答道", "笑道", "听说"):
                        shorter = name[:2]
                        if _valid_name(shorter):
                            name = shorter
                if _valid_name(name):
                    c = res.characters.setdefault(name, Character(name=name))
                    c.mentions += 1
                    c.chapters.add(ch_no)
                    c.named = True

            # ② 分句主语（句首 / 逗号后）+ ③ 说话归属
            for sent in _SEG_RE.findall(text):
                lead = _leading_name(sent)
                if lead:
                    c = res.characters.setdefault(lead, Character(name=lead))
                    c.mentions += 1
                    c.chapters.add(ch_no)
            for m in _SPEAK_RE.finditer(text):
                name = m.group(1)
                if _valid_name(name):
                    c = res.characters.setdefault(name, Character(name=name))
                    c.mentions += 1
                    c.chapters.add(ch_no)
                    c.speaking = True

            # —— 坑线索 ——
            # 判定原则：坑=「读者想知道但还不知道」的信息缺口。
            # 关键词只作候选信号，必须再过「句法形态」闸门，避免金句陈述被当成坑。
            for sent in _SEG_RE.findall(text):
                sent = sent.strip()
                if not sent:
                    continue
                has_q = bool(_QUESTION_MARK_RE.search(sent))
                unfinished = bool(_UNFINISHED_RE.search(sent))
                cue = next((c for c in _PITHOLE_CUES if c in sent), None)
                if not (has_q or unfinished or cue):
                    continue
                if not _is_real_plothole(sent, has_q, unfinished):
                    continue
                key = cue or "疑问"
                ph = res.plotholes.get(key)
                if ph is None:
                    ph = Plothole(key=key, cue=cue or "?", question="")
                    res.plotholes[key] = ph
                if ph.first_chapter is None:
                    ph.first_chapter = ch_no
                ph.last_chapter = ch_no
                # 直接疑问（带？）优先作为悬念原句；否则取首现句
                # 尾部引号剥掉：分句已吃掉配对右引号，但 question 是要注入提示词的
                # 悬念原句，带着收尾引号既无意义又会与提示词自身的引号嵌套。
                if has_q or not ph.question:
                    ph.question = sent.rstrip("”\"'’")

        # —— Pass 1.5：宾语位人名兜底 ——
        # Pass 1 只从「句首主语 / 说话归属 / 显式命名」取候选，导致处于
        # 宾语位的名字不被登记：「林夜拉住苏晚」里苏晚进不了候选表，
        # 于是「林夜拉住苏晚」这条明确交互建不出关系边（实测）。
        # 补一条宾语位线索：动作词/介词紧邻的 2–4 字词，若通过人名校验，
        # 视为候选角色。仍走 Pass 2 的入图门槛，不会引入噪声。
        self._collect_object_names(res, chunks)

        # —— Pass 1.6：粘连体归一（实体消解的规则版）——
        # 「凤姐道」「陆见说」「贾母又」这类「候选角色名 + 说话动词/副词」的粘连体，
        # 若其 2 字前缀**同样是候选角色**，即判定为同一实体的粘连形态：计数并入前缀、
        # 丢弃粘连体。判据刻意用「前缀也是候选」这一**群体证据**而非硬编码姓名表——
        # 反例「叫王守道：“…”」的前缀「王守」不在候选表里，真实 3 字名得以保留。
        # 这同时是 review-A 所指「宁折 ≠ 宁折枝」实体消解问题在规则层的第一个实例：
        # 两种形态各建一个角色 = 角色分裂，消解动作必须在建图前完成。
        self._deglue_candidates(res)

        # —— Pass 1.7：切片伪名消除 ——
        # _leading_name 取分句首 **2 字**，对 3 字名必然切出前缀虚影：
        # 「贾宝玉进了屋」→「贾宝」、「林黛玉低头」→「林黛」、「秦九歌说道」→「秦九」。
        # 虚影只要跨 2 章出现 ≥2 次就能通过 Pass 2 门槛进图，于是图谱里每个 3 字角色
        # 都配一个 2 字分身——这正是 review-A 所指「实体分裂」在规则层的最大规模实例。
        # 判据（群体证据，不硬编码姓名表）：某短名是更长候选名的前缀，且它在全文中
        # **每一次**出现都被那个更长名覆盖（total == covered）→ 它是切片，丢弃；
        # 只要有一次独立出现（total > covered），说明是真实角色，保留（「秦九歌问秦九」）。
        # 必须排在 _deglue_candidates 之后：「宝玉道」这类粘连体已先并回「宝玉」，
        # 否则此处会看到「宝玉」被「宝玉道」完全覆盖而反过来丢掉真名。
        self._drop_shadowed_prefixes(res, chunks)

        # —— Pass 2：入图资格 ——
        final_names = [n for n, c in res.characters.items() if _passes_entry_gate(c)]
        final_set = set(final_names)

        # —— Pass 3：共现计数 ——
        # 原实现对「每个候选名 × 每个分片」做 `name in text`，是 O(名×片) 次子串搜索：
        # 红楼梦实测 400 分片时 4 千候选 → 160 万次搜索，耗时 52s、峰值内存 958MB，
        # 919 分片必然 MemoryError（load_corpus 实测崩在此处）。
        # 改为倒排索引：一次遍历每个分片的全部 2–4 字窗口，边扫边累加，
        # 时间降为 O(总字数)，且不再产生名×片的笛卡尔积。
        if final_names:
            self._sweep_chapters(res, chunks, final_names)

        # —— Pass 4：关系边 ——
        # 原实现以「同分片共现」为关系成立条件，对分片内每两个角色都建一条边，
        # 在 1066 分片 / 1418 角色的语料上产出 138072 条边（其中 62% 只在
        # 单章共现过），是笛卡尔积而非关系，关系查询已无信息量。
        # 改为**有向交互抽取**：只在「角色名 + 邻近关系词」构成的句法窗口内建边，
        # 即必须有「谁 对谁 做了什么」的文本证据，而非同框即连。
        #
        # 候选名取「Pass 1 注册过的全部候选」而非仅 final_names：关系边本身
        # 就是强证据（明确有交互动作），若依赖「跨章出现≥2 次」的角色门槛，
        # 单分片里的真实交互会整段丢失（实测「林夜抓住苏晚」建不出边，
        # 因两人都没达到入图资格）。但仍要求名字能通过关系窗口的扫描。
        # 关系两端必须是**已通过入图门槛的角色**（final_names）。
        # 试过用全部 Pass 1 候选，实测引入「人」这类单字伪角色
        # （红楼梦里有「人说…」，被抽成关系「人—宝玉」50 章），
        # 说明角色资格门槛同时也是关系质量的护栏。
        self._extract_relationships(res, chunks, final_names)

        # 丢弃未入图角色
        res.characters = {n: c for n, c in res.characters.items() if n in final_set}
        return res

    @staticmethod
    def _deglue_candidates(res: ExtractionResult) -> None:
        """把「候选名 + 粘连尾字」的 3 字粘连体并入其 2 字前缀。

        只在**前缀也是候选角色**时执行（群体证据），因此
        - 「凤姐道」+「凤姐」同现 → 合并（真粘连）；
        - 「王守道」独现（前缀「王守」不在候选表）→ 保留（真名）。
        合并会转移 mentions / chapters / named / speaking——`named` 也转移是刻意的：
        「叫凤姐道：」里的「凤姐」确实被显式命名了，丢弃会让该角色退回「未命名」档。

        已知误合并边界：真实 3 字名末字恰为粘连尾字、且其 2 字前缀也恰是另一角色的
        组合（如「秦道」与「秦道先」）会被合并。这类组合需两个名字同时成立，实测
        语料中不构成规模；正式质量由 LLM 抽取后置解决。
        """
        for name in list(res.characters):
            if len(name) != 3 or name[-1] not in _GLUE_TAIL_CHARS:
                continue
            prefix = name[:2]
            if prefix not in res.characters:
                continue
            src = res.characters[name]
            dst = res.characters[prefix]
            dst.mentions += src.mentions
            dst.chapters |= src.chapters
            dst.named = dst.named or src.named
            dst.speaking = dst.speaking or src.speaking
            del res.characters[name]

    @staticmethod
    def _drop_shadowed_prefixes(res: ExtractionResult, chunks: list) -> None:
        """丢弃「每一次出现都被更长候选名覆盖」的切片伪名（前缀虚影）。

        判据是**独立出现次数为 0**，而不是「是否看起来像名字」：
        - 「贾宝」在《红楼梦》里从不独立出现，只出现在「贾宝玉」里 → 虚影，丢弃；
        - 「秦九」若另有「秦九歌问秦九」这种独立出现 → total > covered → 保留。

        成本 O(全文长度)：每个位置只做 2 次 dict 查询 + 命中时才做 startswith，
        不使用「每个候选扫一遍全文」的笛卡尔积写法。

        **为什么覆盖者不按入图闸门过滤（实测决策，勿重复尝试）**：
        本 pass 排在 Pass 2 之前，曾考虑「只用能过闸门的长名当覆盖者」，以免
        「长名稍后被闸门拒 → 这边已经把短名删了 → 两边都没了」。实测否掉了这个改法：
          带闸门过滤   1112 角色 / 70 个切片嫌疑
          不带闸门过滤 1107 角色 / 65 个切片嫌疑   ← 更优，采用
        （切片嫌疑用**不依赖长名是否存在**的独立算子测：某 2 字名 ≥90% 的出现后
          紧跟同一个字，且该 3 字串自身也反复出现。）
        带过滤会放回 5 个切片（朱木/甄士/褚遂/贾宝/花阿），而**一个长名也换不回来**
        —— 朱木兰/甄士隐/褚遂良/花阿珍/贾宝玉 都只在一章出现，闸门本来就会拒。
        即「没有替补」不是本 pass 造成的：有没有替补取决于候选发现能否召回长名。
        真问题在召回（这些长名只在语料里被登记到 1 次）→ 归 LLM 抽取阶段处理。
        本 pass 的契约维持在文本层：短名独立出现次数为 0 即为切片，与长名强弱无关。
        """
        # 收集「某候选是另一更长候选的前缀」的映射（含 2⊂3、2⊂4、3⊂4）
        longer_by_prefix: dict[str, list[str]] = {}
        for n in res.characters:
            for L in range(2, len(n)):
                longer_by_prefix.setdefault(n[:L], []).append(n)
        if not longer_by_prefix:
            return

        total = dict.fromkeys(longer_by_prefix, 0)
        covered = dict.fromkeys(longer_by_prefix, 0)
        for ch in chunks:
            # 覆盖度必须在**去掉汉字间换行**的文本上判定：源文本硬折行会把
            # 「林黛玉」写成「林黛⏎玉」，否则那 17 次会被误判成「林黛」的独立出现，
            # 切片伪名照旧留在图谱里（实测正是漏网原因）。
            text = _unwrap_lines(ch.text or "")
            n_chars = len(text)
            for i in range(n_chars):
                for L in (2, 3):
                    p = text[i:i + L]
                    if len(p) < L:
                        continue
                    longs = longer_by_prefix.get(p)
                    if longs is None:
                        continue
                    total[p] += 1
                    if any(text.startswith(long_, i) for long_ in longs):
                        covered[p] += 1

        for p, t in total.items():
            # t == 0 说明该前缀只在候选表里挂着、全文根本没出现（理论上不该有），
            # 一并丢弃；t == covered 说明每一次出现都被更长名吃掉 → 切片伪名。
            if t == covered[p] and p in res.characters:
                del res.characters[p]

    @staticmethod
    def _collect_object_names(res: ExtractionResult, chunks: list) -> None:
        """宾语位人名兜底：动作词/关系词紧邻的 2–4 字词，通过校验即登记候选。

        只登记候选，不直接入图——入图仍由 Pass 2 的门槛决定，
        所以误判最多浪费一次 setdefault，不会污染图谱。
        """
        for ch in chunks:
            ch_no = ch.chapter_no or 0
            text = ch.text or ""
            for m in _OBJ_TRIGGER_RE.finditer(text):
                tail = text[m.end(): m.end() + 4]
                # 只取尾部连续的汉字前缀（「苏晚的手腕」→「苏晚」），
                # 避免把句号/后续词吃进名字
                cjk = ""
                for chx in tail:
                    if "一" <= chx <= "鿿":
                        cjk += chx
                    else:
                        break
                for L in (3, 2):
                    if len(cjk) < L:
                        continue
                    cand = cjk[:L]
                    # 粘连防护：「见凤姐道：“…”」「叫陆见说道」的宾语位会切出 名+动词
                    # 粘连体（凤姐道/陆见说/宝玉听）——剥离后仍须通过人名校验才采用。
                    # 判据分强弱两档，避免把真实 3 字名一起误伤（「叫王守道：“…”」）：
                    #   ① 强证据：末字 + 后一字构成完整动词短语（陆见说|道 → 说道 ✓）；
                    #   ② 弱证据（冒号归因）：末字后紧接冒号，且**剥出的 2 字前缀
                    #      已是 Pass 1 建立的角色**（凤姐 ✓ / 王守 ✗ 保留原名）。
                    if len(cand) == 3 and cand[-1] in _GLUE_VERB_LEADS:
                        after = tail[3:4]
                        if (cand[2] + after) in ("说道", "问道", "答道", "笑道", "听说"):
                            glued = True
                        elif after in ("：", ":") and cand[:2] in res.characters:
                            glued = True
                        else:
                            glued = False
                        if glued:
                            shorter = cand[:2]
                            if _valid_name(shorter):
                                cand = shorter
                    if _valid_name(cand):
                        c = res.characters.setdefault(
                            cand, Character(name=cand)
                        )
                        c.mentions += 1
                        c.chapters.add(ch_no)
                        break

    @staticmethod
    def _extract_relationships(
        res: ExtractionResult, chunks: list, final_names: list[str]
    ) -> None:
        """按「角色名 + 邻近关系词」抽关系边（有交互证据才连）。

        窗口规则：关系词出现在角色名的同句内，且距该角色名 ≤ REL_WINDOW 字；
        同一句内命中的第一个关系词决定边类型；同框无关系词的组合不建边。

        第二证据类「相邻对话桥接」：网文最高频的对话形态是
        「A 说道：“……”」换行「B 说道：“……”」——两角色分处相邻分句、句内各只有
        一人，同句规则永远看不到两人共现。该形态的结构证据 = 分句恰含 1 个入图
        角色 + 冒号引语（或角色紧贴说话动词），此时该角色即说话人；相邻分句
        说话人不同 → 标「对话」。只连**直接相邻**分句，中间夹叙述句即断开。
        """
        name_set = set(final_names)
        # 长名优先匹配，避免「贾宝玉」被拆成「贾宝」。
        # 同长度必须按名字字典序做二级键：只写 key=len 时，同长名字的先后由 **set 迭代序**
        # 决定，而 set 迭代序随 PYTHONHASHSEED 变化 → 同一语料不同进程跑出不同边数
        # （实测 1224 / 1228 条）。_hits_in 的「跳过被更长名覆盖的短名」依赖此顺序，
        # 于是「确定性、可离线验证」的承诺被破坏。二级键把它钉死。
        ordered = sorted(name_set, key=lambda n: (-len(n), n))
        max_len = max((len(n) for n in name_set), default=2)

        for ch in chunks:
            ch_no = ch.chapter_no or 0
            text = ch.text or ""
            segments = _SEG_RE.findall(text)

            # 逐句处理：一句内的关系词只连该句出现的角色
            for sent in segments:
                if not any(rc in sent for rc in _REL_CUES):
                    continue
                # 定位本句出现的角色（长名优先，跳过被更长名覆盖的短名）
                hits = RuleBasedExtractor._hits_in(sent, ordered)
                if len(hits) < 2:
                    continue
                # 去重后仍需 ≥2 个不同角色（同名多次出现不算两人交互，
                # 否则会建出「宝玉—宝玉」自环）
                if len({n for _, n in hits}) < 2:
                    continue

                # 关系词位置：谁离关系词最近，就把边连到谁
                cue_positions = []
                for rc in _REL_CUES:
                    start = 0
                    while True:
                        i = sent.find(rc, start)
                        if i < 0:
                            break
                        cue_positions.append((i, rc))
                        start = i + 1
                if not cue_positions:
                    continue

                # 每对角色：若存在「两人之间」的线索词才连边（控制规模的关键）
                for x in range(len(hits)):
                    for y in range(x + 1, len(hits)):
                        (i1, n1), (i2, n2) = hits[x], hits[y]
                        lo, hi = min(i1, i2), max(i1, i2)
                        between = sent[lo + 1: hi]
                        if not (1 <= hi - lo <= REL_WINDOW):
                            continue
                        # 两角色之间必须直接出现关系词，或同句内有落在两人之间的线索词
                        rel = next(
                            (rc for rc in _REL_CUES if rc in between), None
                        )
                        if rel is None:
                            near = [
                                rc for pos, rc in cue_positions
                                if lo <= pos <= hi
                            ]
                            rel = near[0] if near else None
                        if rel is None:
                            continue
                        # 边的 key 必须**无序归一化**：早期按出现顺序存
                        # (a,b)，导致「林夜—苏晚」与「苏晚—林夜」各存一条，
                        # 同一关系在图里出现两次。按 (min,max) 归一。
                        # 顺带在这里挡住自环：不同句里同一名字配对仍可能成立
                        # （实测「宝玉—宝玉」10 章），a == b 一律丢弃。
                        if n1 == n2:
                            continue
                        a, b = (n1, n2) if n1 <= n2 else (n2, n1)
                        edge = res.relationships.setdefault(
                            (a, b),
                            Relationship(
                                a=a, b=b, relation=_REL_LABEL.get(rel, rel)
                            ),
                        )
                        edge.chapters.add(ch_no)

            # —— 相邻对话桥接（第二证据类）：A 说… / B 说… 交替 = 对话 ——
            # 与同句规则互补：这里不要求两人共现，而是各句各自归因出唯一说话人，
            # 再要求两人**直接相邻**。宁漏勿错：中间夹叙述句即断开链条。
            speakers = [
                RuleBasedExtractor._dialogue_speaker(s, ordered, name_set)
                for s in segments
            ]
            for i in range(len(speakers) - 1):
                s1, s2 = speakers[i], speakers[i + 1]
                if s1 and s2 and s1 != s2:
                    a, b = (s1, s2) if s1 <= s2 else (s2, s1)
                    edge = res.relationships.setdefault(
                        (a, b), Relationship(a=a, b=b, relation="对话")
                    )
                    edge.chapters.add(ch_no)

    @staticmethod
    def _hits_in(sentence: str, ordered: list[str]) -> list[tuple[int, str]]:
        """定位分句中出现的人物名（长为先，跳过被更长名覆盖的短名）。

        返回 [(位置, 名)]，与关系抽取此前内联的扫描逻辑等价，供同句规则与
        相邻对话桥接共用，避免两处实现漂移。
        """
        hits: list[tuple[int, str]] = []
        taken: list[tuple[int, int]] = []
        for name in ordered:
            L = len(name)
            start = 0
            while True:
                i = sentence.find(name, start)
                if i < 0:
                    break
                if not any(a <= i < a + b for a, b in taken):
                    hits.append((i, name))
                    taken.append((i, L))
                start = i + 1
        return hits

    @staticmethod
    def _dialogue_speaker(
        sentence: str, ordered: list[str], name_set: set[str]
    ) -> str | None:
        """独白式引语归因：分句恰含 1 个入图角色且有引语证据 → 该角色为说话人。

        引语证据二选一：
        ① 冒号 + 引语开头（：“ / :" / ：「），且角色名在冒号**之前**（前置归因）；
        ② 角色名紧贴说话动词（_SPEAK_RE 命中且正是该名，覆盖后置归因「“…”林夜说道。」）。
        句中出现 ≥2 个角色（归因歧义）或无引语证据 → None。

        粘连纠偏：命中名末字是粘连尾巴（说话动词/副词）且其前缀本身也是已入图角色时
        取前缀（「凤姐道」→「凤姐」、「贾母因」→「贾母」）；前缀不在图中则保留原名，
        保护「王守道」这类末字撞车的真实名。
        """
        hits = RuleBasedExtractor._hits_in(sentence, ordered)
        uniq = {n for _, n in hits}
        if len(uniq) != 1:
            return None
        raw = next(iter(uniq))
        only = raw
        if len(raw) >= 3 and raw[-1] in _GLUE_TAIL_CHARS and raw[:-1] in name_set:
            only = raw[:-1]
        m = _SPEECH_QUOTE_RE.search(sentence)
        if m and any(n == raw and i < m.start() for i, n in hits):
            return only
        sm = _SPEAK_RE.search(sentence)
        if sm and sm.group(1) in (raw, only):
            return only
        return None

    @staticmethod
    def _sweep_chapters(
        res: ExtractionResult, chunks: list, final_names: list[str]
    ) -> None:
        """一次遍历全文，用定长滑窗把候选名的出现次数与章节归属补齐。

        角色名长度不一（2–4 字），用「最大长度窗口 + 逐长度取子串」一次覆盖：
        对每个位置取 4 字窗口，若整名不在其中，再依次试 3/2 字。
        """
        name_set = set(final_names)
        max_len = max(len(n) for n in final_names)
        # 长度分组，避免每个位置遍历所有名字
        by_len: dict[int, set[str]] = {}
        for n in name_set:
            by_len.setdefault(len(n), set()).add(n)

        for ch in chunks:
            ch_no = ch.chapter_no or 0
            text = ch.text or ""
            n_chars = len(text)
            for i in range(n_chars):
                # 从长到短匹配：长名优先（「贾宝玉」优先于「贾宝」）
                for L in range(min(max_len, n_chars - i), 1, -1):
                    window = text[i:i + L]
                    if window in name_set:
                        c = res.characters[window]
                        c.mentions += 1
                        c.chapters.add(ch_no)


def resolve_plotholes(result: ExtractionResult, chunks: list) -> None:
    """跨章节结算坑状态：仅在「末现章之后」的章节同时出现强解决短语与该坑关键词才置 resolved。

    - 「末现章之后」排除同章自解析（如「那光是什么？……他才明白」不计为解决）；
    - 同章/后章需同时命中强解决短语（真相是/揭晓/答案是…）与该坑 key，避免其他情节线的
      通用词（明白/原来）误关无关坑；弱词误判率高，留给 LLM 抽取处理。
    """
    for ph in result.plotholes.values():
        last_open = ph.last_chapter or ph.first_chapter or 0
        resolved = False
        for ch in sorted(chunks, key=lambda c: (c.chapter_no or 0)):
            if (ch.chapter_no or 0) > last_open:
                txt = ch.text or ""
                if ph.key in txt and any(rc in txt for rc in _RESOLVE_CUES):
                    resolved = True
                    break
        ph.status = "resolved" if resolved else "open"
