"""CPU count available to this process."""

import math
import os
from pathlib import Path


def _cgroup_limit() -> float | None:
    """CPU limit from the container's cgroup (v2, then v1), or None if unlimited."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError):
        return None


def available_cores() -> int:
    """Cores this process may use: CPU affinity, capped by any container CPU limit.

    ``os.cpu_count()`` can report the host's CPUs inside a container (e.g. Cloud
    Run), which would oversubscribe the instance.
    """
    if hasattr(os, "sched_getaffinity"):
        cores = len(os.sched_getaffinity(0))
    else:
        cores = os.cpu_count() or 1
    limit = _cgroup_limit()
    if limit is not None:
        cores = min(cores, math.ceil(limit))
    return max(1, cores)
