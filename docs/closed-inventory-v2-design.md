# 分维度封闭清单诊断 v2（experimental）

输入：显式 segment_nos 对应既有正文及生成时冻结 authority.premise；可选 inventory_kinds，默认 person/place/object。只写诊断审计及显式配置的检查点，不改正文/权威，不调用 Writer，不 apply。

三个维度独立约束调用，指定维度必须恰好返回一项。缺项失败并保持 unknown；open 也须提供保存权威的精确引用。closed 沿用有限明确封闭措辞守卫，不能证明通用语义完备。

mentions 按字符预算650分组，包含短ID及JSON framing预算。每条仅 source_id/canonical/quote/state，quote上限60字符，服务器定位；没有长reason、kind和坐标。超长单段直接失败，不悄悄省略。每个批次覆盖ID恰好一致且所有引文验证通过后整体接受。

检查点键包含协议版本、完整正文/冻结契约fingerprint、阶段、维度、批次payload；仅成功并校验通过的响应可复用，失败raw独立保存。生产服务内部可传 inventory_checkpoint_dir，公共工具不暴露任意文件路径。正文或契约变化失效。截断仅允许一层二分；真实本轮禁用自动二分，第一次失败即停止。请求上限40；输入token估计为非ASCII字符数+ASCII字符数/4向上取整，仅预算估计，不冒充provider usage。

独立裁决只在所有提及批次通过后执行，每组8候选。完整性分所选维度统计，未选维度列入unreviewed_dimensions；global_quality_verified恒false。覆盖率不证明实体穷尽，候选不等于已核验违例。

验收：真实截断raw拒绝、失败只重做失败阶段、缓存内容指纹失效、既有83段完整离线枚举、一次有界object真实运行；真实完整覆盖和手机/手套违例裁决仍blocked，见scoped-v2审计。
