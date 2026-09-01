"""Process-local, in-memory store for selective-rebuild plans (v2.5).

Deliberately NOT persisted to SQLite or anywhere else durable: a plan is
a short-lived preview between one "build plan" call and one "execute"
call (Design Requirement 22 — synchronous, two-phase, no background
orchestration). This means a plan built on one process is invisible to
a different process's execute call — under `uvicorn --workers N`
(v2.4), plan and execute must land on the same worker. This is a
deliberate, documented limitation, not an oversight — see
docs/DETAILED_GUIDE.md's v2.5 section.
"""

from __future__ import annotations

import threading
import uuid

from app.catalog.rebuild_planner import RebuildPlan

_lock = threading.Lock()
_plans: dict[str, RebuildPlan] = {}


def store_plan(plan: RebuildPlan) -> str:
    plan_id = uuid.uuid4().hex
    with _lock:
        _plans[plan_id] = plan
    return plan_id


def get_plan(plan_id: str) -> RebuildPlan | None:
    with _lock:
        return _plans.get(plan_id)


def discard_plan(plan_id: str) -> None:
    with _lock:
        _plans.pop(plan_id, None)
