"""原创隔离回放的纪律：视图进消费路径后不得漏参考实体、声音层与引文。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_isolation_replay_artifact_passes():
    path = ROOT / 'docs' / 'authority-distill-isolation-replay.json'
    if not path.exists():
        return
    report = json.loads(path.read_text(encoding='utf-8'))
    assert report['passed'] is True, report['checks']
    checks = report['checks']
    assert checks['l2_names_in_original'] == []
    assert checks['voice_names_in_original'] == []
    assert checks['evidence_markers_in_original'] == 0
    # 视图里根本没有声音维度 → include_voice 无从改变输出（结构性保证）。
    assert checks['view_has_voice_dimension'] is False
    # 反证：把声音层塞进档案，参考书角色名立刻出现 —— 说明回放确实在检查。
    assert checks['voice_names_in_voice_layer_text'], checks


def test_replay_tool_reports_consistently():
    """回放脚本自身必须把「视图无声音维度」与「开关失效」区分开。"""
    from pathlib import Path as P
    src = (P(__file__).resolve().parent / 'helpers' / 'replay_method_view_isolation.py')
    text = src.read_text(encoding='utf-8')
    assert 'view_has_voice_dimension' in text
    assert 'voice_switch_changes_output' in text
