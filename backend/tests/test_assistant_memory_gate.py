"""长期记忆抽取闸门：问句不能被「不能 / 不要」这类子串误判成偏好陈述。

用户实测：「能不能加入修仙境界？」被抽进记忆却没有回答。根因是
`_PREFERENCE_SIGNALS` 里的「不能」命中了「能不能」，在问号判断之前直接返回 True。
"""
from __future__ import annotations

from app.assistant_memory import should_extract


def test_interrogative_with_buneng_is_not_extracted():
    assert should_extract("能不能加入修仙境界？") is False
    assert should_extract("能不能加入修仙境界") is False
    assert should_extract("可不可以把主角改成女性？") is False


def test_real_preference_statement_is_extracted():
    assert should_extract("以后不能写电话号码") is True
    assert should_extract("这本书始终用第一人称") is True
    # 夹着疑问词、但整体仍是偏好陈述：偏好词优先，不能误杀。
    assert should_extract("以后每次怎么写都要用第一人称") is True


def test_lookup_request_is_not_extracted():
    assert should_extract("帮我查一下第 3 段写了什么") is False
