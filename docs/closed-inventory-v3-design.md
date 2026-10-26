# 封闭清单 v3 短实体定位与有限恢复（experimental）

输入沿用 v2 冻结正文/authority；输出增加 rejected_mentions、逐source未解决项、定位数组和裁决范围。模型只选择短ID、逐字surface、canonical、独立state/relation，可选1-based occurrence；服务端保留全部匹配或明确指定匹配，并用真实原段生成上下文quote与绝对offset。不修复原文或模型实体。

v1/v2保持可回放。v3检查点绑定版本与完整输入hash；可显式读取匹配输入、按旧validator复核成功的v2约束及完整批次，保留旧key/协议追溯。失败raw绝不升级为成功。每个mention单独拒绝，只将涉及source标记不完整；未知source使批次覆盖不完整。结构/API失败停止剩余mentions，允许已验证账本进入一次限定裁决。

独立裁决携带body_source_id及authority_source_id，服务端匹配候选两端并验证真实来源、closed权限及定位；词差集仅产生候选。现实状态和关系由独立模型复核，非事实不自动升级违规。一次裁决允许选定候选source范围，其余候选明确未裁决。完整coverage要求全部source无未解决；局部裁决不宣称全局通过。

验收：真实失败raw离线仍拒绝；新协议控制响应单列测试；多次定位/非法occurrence、跨题材否定假设比喻、检查点hash失效及旧24段复用；真实调用仅剩余mentions与一次object裁决，Writer=0/apply=false，原数据库hash/stat不变。保持实验版，不声称跨题材召回已验证。
