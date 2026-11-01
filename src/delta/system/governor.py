"""Resource governor: samples CPU/RAM/GPU/temps/battery and decides how hard Delta may push the laptop.

Other components ask it `allow_local()` before running local inference, and subscribe to
health changes (e.g. the orchestrator pauses on CRITICAL).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

import psutil

from delta.config import GovernorConfig
from delta.system.hardware_guard import Assessment, Health, Snapshot, assess
from delta.system.logging import log


class Sensor(Protocol):
    def read(self) -> Snapshot: ...


class SystemSensor:
    """psutil + NVML (nvidia-ml-py). Every reading is best-effort; missing sensors are None."""

    def __init__(self) -> None:
        self._nvml_handle = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            self._nvml = None
        psutil.cpu_percent(None)  # prime the counter so the first read is meaningful

    def read(self) -> Snapshot:
        gpu_util = gpu_temp = vram = None
        if self._nvml and self._nvml_handle is not None:
            try:
                n, h = self._nvml, self._nvml_handle
                gpu_util = float(n.nvmlDeviceGetUtilizationRates(h).gpu)
                gpu_temp = float(n.nvmlDeviceGetTemperature(h, n.NVML_TEMPERATURE_GPU))
                mem = n.nvmlDeviceGetMemoryInfo(h)
                vram = 100.0 * mem.used / mem.total
            except Exception:
                pass
        mem = psutil.virtual_memory()
        battery = psutil.sensors_battery()
        cpu_temp = None
        temps = getattr(psutil, "sensors_temperatures", None)  # not available on Windows
        if temps:
            with contextlib.suppress(Exception):
                readings = [t.current for group in temps().values() for t in group]
                cpu_temp = max(readings) if readings else None
        return Snapshot(
            cpu_pct=psutil.cpu_percent(None),
            ram_pct=mem.percent,
            ram_free_gb=mem.available / 1e9,
            cpu_temp_c=cpu_temp,
            gpu_util_pct=gpu_util,
            gpu_temp_c=gpu_temp,
            vram_pct=vram,
            battery_pct=battery.percent if battery else None,
            plugged=battery.power_plugged if battery else True,
        )

    def close(self) -> None:
        if self._nvml:
            with contextlib.suppress(Exception):
                self._nvml.nvmlShutdown()


HealthListener = Callable[[Assessment], Awaitable[None]]


class Governor:
    def __init__(self, cfg: GovernorConfig, sensor: Sensor | None = None) -> None:
        self.cfg = cfg
        self.sensor = sensor or SystemSensor()
        self.snapshot = Snapshot()
        self.assessment = Assessment(Health.NORMAL)
        self._listeners: list[HealthListener] = []
        self._task: asyncio.Task[None] | None = None

    def on_change(self, listener: HealthListener) -> None:
        self._listeners.append(listener)

    def allow_local(self) -> bool:
        """Local GPU inference is allowed only when the machine is healthy."""
        return self.assessment.health == Health.NORMAL

    @property
    def health(self) -> Health:
        return self.assessment.health

    async def sample(self) -> Assessment:
        self.snapshot = await asyncio.to_thread(self.sensor.read)
        new = assess(self.snapshot, self.cfg.limits)
        if new.health != self.assessment.health:
            log.warning("governor.health_changed", health=new.health.name, reasons=new.reasons)
            self.assessment = new
            for listener in self._listeners:
                await listener(new)
        self.assessment = new
        return new

    async def start(self) -> None:
        if self.cfg.below_normal_priority:
            with contextlib.suppress(Exception):
                p = psutil.Process()
                p.nice(getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", 10))
        await self.sample()
        self._task = asyncio.create_task(self._loop(), name="governor")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.sample_interval_s)
            try:
                await self.sample()
            except Exception as e:  # never let monitoring kill the agent
                log.error("governor.sample_failed", error=str(e))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        close = getattr(self.sensor, "close", None)
        if close:
            close()

    def status(self) -> dict[str, object]:
        s = self.snapshot
        return {
            "health": self.health.name,
            "reasons": self.assessment.reasons,
            "cpu_pct": s.cpu_pct,
            "ram_pct": s.ram_pct,
            "ram_free_gb": None if s.ram_free_gb is None else round(s.ram_free_gb, 1),
            "gpu_util_pct": s.gpu_util_pct,
            "gpu_temp_c": s.gpu_temp_c,
            "vram_pct": None if s.vram_pct is None else round(s.vram_pct, 1),
            "battery_pct": s.battery_pct,
            "plugged": s.plugged,
            "process_rss_mb": round(psutil.Process().memory_info().rss / 1e6, 1),
        }
