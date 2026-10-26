"""Reconcile dated course reminders with AstrBot's public CronJobManager API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .core import bounded_int, due_reminders, next_reminders, reminder_key
from .task_text import render_requirement, validate_requirement

OWNER = "astrbot_plugin_classnote"
HORIZON_DAYS = 7
PER_BINDING_LIMIT = 200
TOTAL_LIMIT = 1000


class FutureTasks:
    def __init__(self, plugin):
        self.plugin = plugin
        self._bound_manager = None
        self.lock = asyncio.Lock()
        self.jobs = []
        self.last_sync = None
        self.error = ""
        self.truncated = False

    @property
    def manager(self):
        return getattr(self.plugin.context, "cron_manager", None)

    @property
    def effective_mode(self):
        requested = self.plugin.config.get("scheduler_mode", "future_tasks")
        return (
            "future_tasks"
            if requested == "future_tasks" and self.available
            else "polling"
        )

    @property
    def available(self):
        return self.manager is not None and all(
            callable(getattr(self.manager, name, None))
            for name in ("add_basic_job", "update_job", "delete_job", "list_jobs")
        )

    @staticmethod
    def owned(job):
        payload = getattr(job, "payload", None)
        return isinstance(payload, dict) and payload.get("classnote_owner") == OWNER

    @staticmethod
    def capture_requirement(job, record):
        """Retain edits made in AstrBot's native task editor as per-task overrides."""
        rendered = record.get("rendered_requirement")
        if rendered is not None and job.description != rendered and job.description:
            record["requirement_override"] = validate_requirement(job.description)

    async def requirement(self, binding, item):
        key = item.get("key") or reminder_key(
            binding, item, item.get("lead", binding["lead_minutes"][0])
        )
        record = self.plugin.store.tasks().get(key, {})
        if self.available and self.effective_mode == "future_tasks":
            for job in await self.manager.list_jobs():
                if self.owned(job) and job.payload.get("key") == key:
                    self.capture_requirement(job, record)
                    break
        return record.get("requirement_override") or render_requirement(
            self.plugin.config, binding, item
        )

    async def callback(self, binding_id, key, epoch, revision, **_):
        """Revalidate the live course at execution time, then use the shared outbox."""
        if self.effective_mode != "future_tasks":
            return
        await self.plugin._apply_binding_defaults()
        binding = self.plugin.store.get(binding_id)
        if (
            not binding
            or binding.get("epoch") != epoch
            or binding["revision"] != revision
            or not self.plugin._active(binding)
        ):
            return
        now = datetime.now(timezone.utc)
        grace = bounded_int(
            self.plugin.config.get("catchup_seconds", 300), 30, 900, "补发窗口"
        )
        for item in due_reminders(binding, now, grace):
            if item["key"] == key:
                async with self.plugin.delivery_semaphore:
                    if not await self.plugin._deliver(binding, item):
                        record = self.plugin.store.delivery(key)
                        if record and record["state"] == "failed":
                            raise RuntimeError(
                                "ClassNote delivery failed; bounded retry will be scheduled"
                            )
                return

    async def sync(self, *, rebind=False, reset_deleted=False):
        """Create/update only owned jobs, preserving pauses and explicit deletions.

        Basic job callbacks are process-local in AstrBot. On startup, rebuild our
        persisted jobs through public APIs, preserving each job's enabled state.
        No private scheduler/handler registry is accessed.
        """
        await self.plugin._apply_binding_defaults()
        async with self.lock:
            if not self.available:
                self.error = (
                    "当前 AstrBot 未提供未来任务调度接口，已自动使用插件轮询提醒。"
                )
                self.jobs = []
                return
            try:
                await self._sync(
                    rebind=rebind or self._bound_manager is not self.manager,
                    reset_deleted=reset_deleted,
                )
                self._bound_manager = self.manager
                self.last_sync = datetime.now(timezone.utc).isoformat()
                self.error = ""
            except Exception as exc:
                self.error = (
                    f"未来任务同步失败（{type(exc).__name__}），请检查日志后重试。"
                )
                raise

    async def _sync(self, *, rebind, reset_deleted):
        now = datetime.now(timezone.utc)
        jobs = [job for job in await self.manager.list_jobs() if self.owned(job)]
        known = self.plugin.store.tasks()
        desired = {}
        self.truncated = False
        if self.plugin.config.get("scheduler_mode", "future_tasks") == "future_tasks":
            for binding in self.plugin.store.all():
                if not self.plugin._active(binding):
                    continue
                grace = bounded_int(
                    self.plugin.config.get("catchup_seconds", 300), 30, 900, "补发窗口"
                )
                items = due_reminders(binding, now, grace) + next_reminders(
                    binding, now, PER_BINDING_LIMIT + 1
                )
                in_range = [
                    item
                    for item in items
                    if item["fire"] <= now + timedelta(days=HORIZON_DAYS)
                ]
                if len(in_range) > PER_BINDING_LIMIT:
                    self.truncated = True
                for item in in_range[:PER_BINDING_LIMIT]:
                    key = item.get("key") or reminder_key(binding, item, item["lead"])
                    item["key"] = key
                    record = self.plugin.store.delivery(key)
                    if record and (
                        record["state"] == "sent" or record["attempts"] >= 3
                    ):
                        continue
                    desired[key] = (binding, item, record)
                    if len(desired) >= TOTAL_LIMIT:
                        self.truncated = True
                        break
                if len(desired) >= TOTAL_LIMIT:
                    break
        by_key = {}
        for job in jobs:
            key = job.payload.get("key")
            target = desired.get(key)
            stale = not target or job.payload.get("epoch") != target[0].get("epoch")
            if stale or key in by_key:
                await self.manager.delete_job(job.job_id)
                # Keep explicit deletion tombstones, but not removed schedules.
                if key in known and not known[key].get("suppressed"):
                    self.plugin.store.forget_task(key)
                    known.pop(key, None)
                continue
            by_key[key] = job
        for key, (binding, item, record) in desired.items():
            old = known.get(key, {})
            job = by_key.get(key)
            signature = [binding.get("epoch"), binding["revision"]]
            if old.get("signature", [None])[0] != signature[0]:
                old = {}
            if job:
                self.capture_requirement(job, old)
            description = old.get("requirement_override") or render_requirement(
                self.plugin.config, binding, item
            )
            old["rendered_requirement"] = description
            suppressed = old.get("suppressed", False) and not reset_deleted
            # A missing pending job was deleted in AstrBot's task page. Do not
            # silently recreate it. Failed/claimed jobs may need crash recovery.
            if old.get("job_id") and not job and not record and not reset_deleted:
                suppressed = True
            if suppressed:
                self.plugin.store.save_task(
                    key,
                    {
                        **old,
                        "binding_id": binding["id"],
                        "signature": signature,
                        "suppressed": True,
                    },
                )
                continue
            enabled = bool(job.enabled) if job else old.get("enabled", True)
            name = f"课笺 · {item['name']} · {item['begin']:%m/%d %H:%M} · 提前{item['lead']}分钟"
            if job and not rebind:
                payload = {
                    **job.payload,
                    "revision": binding["revision"],
                    "begin": item["begin"].isoformat(),
                }
                if (
                    job.description != description
                    or job.payload != payload
                    or job.timezone != binding["timezone"]
                    or job.name != name
                ):
                    updated = await self.manager.update_job(
                        job.job_id,
                        description=description,
                        payload=payload,
                        timezone=binding["timezone"],
                        name=name,
                    )
                    if updated is None:
                        raise RuntimeError("Native task disappeared during update")
                self.plugin.store.save_task(
                    key,
                    {
                        **old,
                        "binding_id": binding["id"],
                        "job_id": job.job_id,
                        "signature": signature,
                        "enabled": enabled,
                    },
                )
                continue
            if job:
                await self.manager.delete_job(job.job_id)
            run_at = item["fire"]
            if record:
                # AstrBot removes one-shot jobs even after failures. Retry through
                # a new native job, never through a second delivery pipeline.
                delay = 120 if record["state"] == "sending" else 30
                run_at = max(
                    run_at,
                    datetime.fromtimestamp(record["updated"], timezone.utc)
                    + timedelta(seconds=delay),
                )
            if run_at < now:
                run_at = now + timedelta(seconds=2)
            if run_at >= item["begin"]:
                continue
            payload = {
                "classnote_owner": OWNER,
                "binding_id": binding["id"],
                "key": key,
                "epoch": binding.get("epoch"),
                "revision": binding["revision"],
                "session": binding["umo"],
                "run_at": run_at.isoformat(),
                "course": item["name"],
                "begin": item["begin"].isoformat(),
                "lead": item["lead"],
            }
            job = await self.manager.add_basic_job(
                name=name,
                cron_expression="* * * * *",
                handler=self.callback,
                description=description,
                timezone=binding["timezone"],
                payload=payload,
                enabled=False,
                persistent=True,
            )
            # The public basic-job factory only accepts cron. Create disabled,
            # then atomically enable it as a DateTrigger using update_job.
            job_id = job.job_id
            try:
                job = await self.manager.update_job(
                    job_id, run_once=True, payload=payload, enabled=enabled
                )
                if job is None:
                    raise RuntimeError("Native task disappeared during creation")
            except BaseException:
                await self.manager.delete_job(job_id)
                raise
            self.plugin.store.save_task(
                key,
                {
                    **old,
                    "binding_id": binding["id"],
                    "job_id": job.job_id,
                    "signature": signature,
                    "enabled": enabled,
                },
            )
            by_key[key] = job
        for key, old in known.items():
            if key not in desired:
                self.plugin.store.forget_task(key)
        self.jobs = [job for job in await self.manager.list_jobs() if self.owned(job)]

    async def close(self):
        """Unschedule owned jobs on unload; retain preferences for a later reload."""
        if not self.available:
            return
        async with self.lock:
            for job in await self.manager.list_jobs():
                if self.owned(job):
                    key = job.payload.get("key")
                    body = self.plugin.store.tasks().get(key, {})
                    self.capture_requirement(job, body)
                    body.update(
                        binding_id=job.payload["binding_id"], enabled=job.enabled
                    )
                    body.pop("job_id", None)
                    self.plugin.store.save_task(key, body)
                    await self.manager.delete_job(job.job_id)
            self.jobs = []
            self._bound_manager = None

    def status(self):
        records = self.plugin.store.tasks() if self.plugin.store else {}
        return {
            "available": self.available,
            "mode": self.effective_mode,
            "requested_mode": self.plugin.config.get("scheduler_mode", "future_tasks"),
            "last_sync": self.last_sync,
            "error": self.error,
            "horizon_days": HORIZON_DAYS,
            "truncated": self.truncated,
            "count": len(self.jobs),
            "jobs": [
                {
                    "id": job.job_id,
                    "key": job.payload.get("key"),
                    "binding_id": job.payload.get("binding_id"),
                    "name": job.name,
                    "enabled": job.enabled,
                    "fire": job.payload.get("run_at"),
                    "begin": job.payload.get("begin"),
                    "course": job.payload.get("course"),
                    "requirement": job.description or "",
                    "custom_requirement": bool(
                        records.get(job.payload.get("key"), {}).get(
                            "requirement_override"
                        )
                    ),
                    "last_error": getattr(job, "last_error", None),
                }
                for job in self.jobs
            ],
        }
