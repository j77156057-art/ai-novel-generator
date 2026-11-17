"""B4：单项目生成租约（并发/双击闸门）的单元回归。"""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.generation import inflight  # noqa: E402
from app.routers.generation import _begin_generation  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    inflight.reset()
    yield
    inflight.reset()


def test_second_begin_is_rejected():
    assert inflight.try_begin(1) is True
    assert inflight.try_begin(1) is False
    assert inflight.is_in_flight(1) is True

    inflight.end(1)
    assert inflight.is_in_flight(1) is False
    assert inflight.try_begin(1) is True


def test_different_projects_are_independent():
    assert inflight.try_begin(1) is True
    assert inflight.try_begin(2) is True
    inflight.end(1)
    assert inflight.try_begin(2) is False


def test_stale_lease_can_be_taken_over():
    assert inflight.try_begin(3, now=0.0) is True
    assert inflight.try_begin(3, now=inflight.LEASE_SECONDS + 1) is True


def test_router_guard_returns_409_when_busy():
    assert inflight.try_begin(9) is True
    with pytest.raises(HTTPException) as err:
        _begin_generation(9)
    assert err.value.status_code == 409
