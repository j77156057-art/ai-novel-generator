docs: 真链路 percentile 100% 触发架构级根因调查（unit_chars 粒度不对齐）

**8 轮真链路冒烟 + 1 轮 probe 一致结论**：
- v2.7-v2.10 智谱 250414 percentile 100/100
- d56f988 v2.12 nemotron-3-super 真链路 percentile 100/100
- probe 0cca142 nemotron 单条 200 字 prompt 145.8s 写 3691 字**文风干净**

**根因 = style_distance 粒度不对齐**（不是单模型文风问题）：

1. `fit_profile(unit_chars=3000)` —— 留出集切成 3000 字/单元统计 mean/std
2. v2.10 智谱段 1 6264 字（scale_ratio 2.09x）/ v2.10 段 2 4747 字（1.58x）
3. v2.12 nemotron 段 2 2841 字（0.95x，但仍 100%）
4. `style_distance` line 481 自己注释：「`scale_ratio` 粒度不对齐时距离会系统性偏大」
5. features 在不同长度上算密度（省略号/100字、句号/100字、虚词/100字）——
   3000 字留出集统计的 mean/std **不能直接对 6000 字段算 z**
6. 智谱段 1 mean_abs_z = 3.476，nemotron 段 2 = 9.165（v2.11 v1 89 字 fallback）——
   都远超参考留出集分布（p90 通常 0.5-1.5），触发 100% percentile

**关键证据** —— `style_distance` 函数 docstring 早就承认这个问题：
> 粒度不对齐时距离会系统性偏大，必须如实标注而不是静默给个数字
> `scale_ratio`: round(len(text) / unit_chars, 2) if unit_chars else None

但代码**只是标注 scale_ratio**，**没有用它做修正**——所以 scale_ratio = 2.09x 时 percentile 仍按 z 算 → 100%。

**修法（候选 I 待做）**：
- 方案 A（轻）：`style_distance` 检测 scale_ratio 超 0.7-1.5 范围 → 把 text 切 unit_chars 段，每段单独算 distance，取 max 触发告警（最严）或 mean（宽松）
- 方案 B（中）：profile fit 时按**对数粒度**切（unit_chars/2 + unit_chars + unit_chars*2 各 fit 一份）→ 生成段按 length 选最匹配 profile
- 方案 C（重）：改 features 全部归一化（除以 log(len) 或 sqrt(len)）—— 长度不敏感

**推荐方案 A**（最小改动）—— `style_distance` 内部把长文切成多段，每段单独 distance，取均值 percentile。

**probe 路径 145.8s 写 3691 字为什么「文风干净」** —— 因为 probe 是单条 prompt 路径，**没走 engine 的 _style_profile()**，所以**没算 percentile**。真链路 100% 触发完全是 engine 的 style_distance 算错。

**颠覆之前结论**：
- 「智谱文风服从性差」—— 不对，**真链路算错**
- 「换模型根除 percentile 100%」—— 不对，**nemotron 也 100%**
- 「probe 145.8s 3691 字文风干净」—— 是因为没算 percentile

**下一步建议**：
- 实现方案 A + 加 test（生成 3000/6000/9000 字测试，percentile 应随长度回归到 < 97.5）
- 修后重跑 v2.7-v2.10 + v2.12 看 percentile 是否真降
- 真正对话写小说（用户跑端到端）—— 工程意义大于冒烟
