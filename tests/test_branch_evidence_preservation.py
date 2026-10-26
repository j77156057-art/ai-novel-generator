"""Branch attachment must preserve causal completion evidence."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.generation.timeline import plan_timeline, PlotNode
from app.generation.branch import attach_branch_to_state

def test_branch_attachment_keeps_completion_and_judged_segment():
    st=plan_timeline(6)
    st.completion={'n1':{'verdict':'supported','sources':[{'quote':'已核对记录'}]}}
    st.last_judged_segment=1
    branch=PlotNode('b1','branch','核对副本','悬疑','记录尚未对齐','副本已对齐',parent='n1')
    result=attach_branch_to_state(st,branch)
    assert result.completion==st.completion
    assert result.last_judged_segment==1
