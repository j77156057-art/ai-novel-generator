# 蒸馏语料绑定

项目缓存目前在计算正文哈希前返回，导致同 novel_id 重切/清洗后复用旧档案。共享库虽有正文哈希，但缺少版本和清洗流水线绑定；预览也重复相同漏洞。

以实际持久化 chunk（novel_id、chapter_no、idx、text，确定顺序）及每本 clean_report 的 pipeline_version/consumption_version/cleaned_fingerprint 为输入，生成稳定摘要。不能信任报告自述的 chunks_fingerprint 替代实际正文。项目 fingerprint 增加 corpus_hash；共享库 content_hash 使用同一摘要，并要求 narrative_version 当前。无绑定旧缓存失效。预览与执行共用口径。

验收：正文原地变化失效；清洗版本变化失效；不变时零模型复用；旧共享档案不得重新标为当前。只在隔离库真实验证，不更改现用服务。规则手法与实体事实仍分层，未知污染保留待复查。
