from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .executor import execute_opencli_job
from .models import Lane, PoolConfig
from .store import JobStore

logger = logging.getLogger("opencli_service")


class OpenCLIService:
    def __init__(
        self,
        *,
        store: JobStore,
        default_profile: str,
        default_pool: PoolConfig,
        pools: dict[str, PoolConfig],
        poll_seconds: float,
    ) -> None:
        self.store = store
        self.default_profile = default_profile
        self.default_pool = default_pool
        self.pools = pools
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lanes: dict[tuple[str, str], list[Lane]] = {}
        self._guard = threading.Lock()

    def start(self) -> None:
        for site in sorted(self.pools):
            self.ensure_pool(self.default_profile, site)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def ensure_pool(self, profile: str, site: str) -> None:
        key = (profile, site)
        with self._guard:
            if key in self._lanes:
                return
            pool = self.pools.get(site, self.default_pool)
            lanes = [
                Lane(profile=profile, site=site, index=index, timeout_seconds=pool.timeout_seconds)
                for index in range(pool.lanes)
            ]
            self._lanes[key] = lanes
            for lane in lanes:
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(lane,),
                    name=f"opencli-{lane.id}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            logger.info("started opencli pool %s/%s with %d lane(s)", profile, site, pool.lanes)

    def known_pools(self) -> dict[str, Any]:
        with self._guard:
            return {
                f"{profile}:{site}": {
                    "profile": profile,
                    "site": site,
                    "lanes": [lane.id for lane in lanes],
                }
                for (profile, site), lanes in sorted(self._lanes.items())
            }

    def _worker_loop(self, lane: Lane) -> None:
        while not self._stop.is_set():
            job = self.store.acquire_next(profile=lane.profile, site=lane.site, lane_id=lane.id)
            if job is None:
                self._stop.wait(self.poll_seconds)
                continue
            job_id = str(job["id"])
            logger.info("lane %s running job %s %s/%s", lane.id, job_id, job["site"], job["command"])
            result = execute_opencli_job(job, lane)
            if result.returncode == 0 and result.error is None:
                self.store.finish(
                    job_id,
                    status="succeeded",
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    result=result.parsed,
                )
                logger.info("lane %s succeeded job %s", lane.id, job_id)
                continue
            message = result.error or result.stderr[:500] or result.stdout[:500] or f"exit={result.returncode}"
            self.store.finish(
                job_id,
                status="failed",
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                result=result.parsed,
                error=message,
            )
            logger.warning("lane %s failed job %s: %s", lane.id, job_id, message)

    def wake_for(self, profile: str, site: str) -> None:
        self.ensure_pool(profile, site)
