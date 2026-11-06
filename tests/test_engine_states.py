"""The task state machine: only the listed transitions are possible, enforced in code by TaskStore."""

import pytest

from delta.engine.states import FINAL, RUNNING, TRANSITIONS, IllegalTransitionError, TaskState, can_transition
from delta.engine.store import TaskStore

S = TaskState


def test_every_state_has_a_rule_and_final_states_are_final():
    assert set(TRANSITIONS) == set(TaskState)
    for state in FINAL:
        assert TRANSITIONS[state] == frozenset(), state
    for state in RUNNING:  # whatever runs can be cut off by a crash
        assert S.INTERRUPTED in TRANSITIONS[state]


@pytest.mark.parametrize(("current", "new"), [
    (S.QUEUED, S.PLANNING), (S.PLANNING, S.ACTING), (S.ACTING, S.VERIFYING), (S.VERIFYING, S.ACTING),
    (S.VERIFYING, S.DONE), (S.ACTING, S.WAITING_USER), (S.WAITING_USER, S.PLANNING), (S.ACTING, S.INTERRUPTED),
    (S.INTERRUPTED, S.QUEUED), (S.INTERRUPTED, S.CANCELLED), (S.PLANNING, S.FAILED),
])
def test_allowed(current, new):
    assert can_transition(current, new)


@pytest.mark.parametrize(("current", "new"), [
    (S.DONE, S.ACTING), (S.FAILED, S.QUEUED), (S.CANCELLED, S.PLANNING), (S.INTERRUPTED, S.ACTING),
    (S.INTERRUPTED, S.DONE), (S.WAITING_USER, S.INTERRUPTED), (S.DONE, S.INTERRUPTED), (S.QUEUED, "nonsense"),
])
def test_refused(current, new):
    assert not can_transition(current, new)


def test_store_enforces_transitions(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.create("t1", S.QUEUED, text="hello")
    store.transition("t1", S.PLANNING)
    store.transition("t1", S.PLANNING)  # same state: no-op, no error
    store.transition("t1", S.ACTING)
    store.transition("t1", S.DONE)
    assert store.get("t1")["state"] == "done" and store.get("t1")["finished"]
    with pytest.raises(IllegalTransitionError):
        store.transition("t1", S.ACTING)
    with pytest.raises(KeyError):
        store.transition("missing", S.PLANNING)
    states = [e["summary"] for e in store.events("t1", ["state"])]
    assert states == ["-> queued", "queued -> planning", "planning -> acting", "acting -> done"]


def test_interrupted_can_only_be_resumed_or_dropped(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.create("t2", S.ACTING)
    store.transition("t2", S.INTERRUPTED, "crash")
    for bad in (S.ACTING, S.PLANNING, S.DONE, S.VERIFYING):
        with pytest.raises(IllegalTransitionError):
            store.transition("t2", bad)
    store.transition("t2", S.QUEUED)
    assert store.get("t2")["state"] == "queued"
