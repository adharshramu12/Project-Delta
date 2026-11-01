"""Pure hardware-health evaluation. No I/O here so it is trivially testable."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from delta.config import Limit


class Health(IntEnum):
    NORMAL = 0
    THROTTLE = 1  # stop local inference, prefer cloud, slow down screen capture
    CRITICAL = 2  # pause the agent and tell the user


@dataclass(frozen=True)
class Snapshot:
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    cpu_temp_c: float | None = None
    gpu_util_pct: float | None = None
    gpu_temp_c: float | None = None
    vram_pct: float | None = None
    battery_pct: float | None = None
    plugged: bool = True


@dataclass(frozen=True)
class Assessment:
    health: Health
    reasons: list[str] = field(default_factory=list)


DEFAULT_LIMITS = {
    "gpu_temp_c": Limit(throttle=78, critical=87),
    "cpu_temp_c": Limit(throttle=90, critical=97),
    "vram_pct": Limit(throttle=85, critical=95),
    "ram_pct": Limit(throttle=85, critical=93),
    "battery_pct_unplugged": Limit(throttle=30, critical=15),
}


def assess(snap: Snapshot, limits: dict[str, Limit] | None = None) -> Assessment:
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    health = Health.NORMAL
    reasons: list[str] = []

    def bump(level: Health, why: str) -> None:
        nonlocal health
        health = max(health, level)
        reasons.append(why)

    for key in ("gpu_temp_c", "cpu_temp_c", "vram_pct", "ram_pct"):
        value = getattr(snap, key)
        if value is None:
            continue
        if value >= lim[key].critical:
            bump(Health.CRITICAL, f"{key}={value:.0f} >= {lim[key].critical:.0f}")
        elif value >= lim[key].throttle:
            bump(Health.THROTTLE, f"{key}={value:.0f} >= {lim[key].throttle:.0f}")

    if snap.battery_pct is not None and not snap.plugged:
        bl = lim["battery_pct_unplugged"]
        if snap.battery_pct <= bl.critical:
            bump(Health.CRITICAL, f"battery {snap.battery_pct:.0f}% on battery power")
        elif snap.battery_pct <= bl.throttle:
            bump(Health.THROTTLE, f"battery {snap.battery_pct:.0f}% on battery power")

    return Assessment(health, reasons)
