"""Background services: start/stop the cron scheduler with the app lifespan."""

from __future__ import annotations


async def start_cron() -> None:
    from . import get_cron_service

    await get_cron_service().start()


async def stop_cron() -> None:
    from . import get_cron_service

    await get_cron_service().stop()
