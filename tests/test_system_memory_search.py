import pytest

from delta.config import GovernorConfig
from delta.memory.store import MemoryStore
from delta.system.governor import Governor
from delta.system.hardware_guard import Health, Snapshot, assess
from delta.tools.web_search import SearchError, SearchHit, SearchRouter


def test_hardware_guard_levels():
    assert assess(Snapshot(gpu_temp_c=60, vram_pct=40)).health == Health.NORMAL
    assert assess(Snapshot(gpu_temp_c=80)).health == Health.THROTTLE
    assert assess(Snapshot(gpu_temp_c=90)).health == Health.CRITICAL
    assert assess(Snapshot(battery_pct=25, plugged=False)).health == Health.THROTTLE
    assert assess(Snapshot(battery_pct=25, plugged=True)).health == Health.NORMAL


class HotSensor:
    def __init__(self):
        self.temp = 60.0

    def read(self):
        return Snapshot(gpu_temp_c=self.temp)


async def test_governor_blocks_local_when_hot():
    sensor = HotSensor()
    gov = Governor(GovernorConfig(below_normal_priority=False), sensor)
    changes = []

    async def listener(a):
        changes.append(a.health)

    gov.on_change(listener)
    await gov.sample()
    assert gov.allow_local()
    sensor.temp = 82
    await gov.sample()
    assert not gov.allow_local()
    assert changes == [Health.THROTTLE]


def test_memory_add_dedupe_search_delete(tmp_path):
    m = MemoryStore(tmp_path / "m.db")
    a = m.add("Prefers Antigravity for Python projects")
    assert m.add("prefers antigravity for python projects") == a
    m.add("Works on project delta", kind="episode")
    hits = m.search("open my python editor")
    assert hits and hits[0].id == a
    assert "Antigravity" in m.context_for("python")
    assert m.delete(a)
    assert m.search("antigravity") == []


class FakeSearch:
    def __init__(self, name, hits=None, fail=False, has_key=True):
        self.name, self.hits, self.fail, self.has_key = name, hits or [], fail, has_key
        self.calls = 0

    def available(self):
        return self.has_key

    async def search(self, query, n):
        self.calls += 1
        if self.fail:
            raise SearchError(f"{self.name}: HTTP 432 out of credits")
        return self.hits


async def test_search_falls_back_to_duckduckgo_and_cools_down():
    tavily = FakeSearch("tavily", fail=True)
    serp = FakeSearch("serpapi", has_key=False)
    ddg = FakeSearch("duckduckgo", [SearchHit("t", "https://x", "s")])
    r = SearchRouter([tavily, serp, ddg])
    assert (await r.search("q"))[0] == "duckduckgo"
    await r.search("q")
    assert tavily.calls == 1  # out of credits -> skipped during cool-down
    assert serp.calls == 0


async def test_search_all_fail():
    with pytest.raises(SearchError):
        await SearchRouter([FakeSearch("a", fail=True)]).search("q")
