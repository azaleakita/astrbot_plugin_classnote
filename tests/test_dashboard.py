"""Behavioral contracts for the native scheduler and authenticated dashboard API."""

import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from astrbot_plugin_classnote.core import TimetableError
from astrbot_plugin_classnote.ics import parse_calendar
from astrbot_plugin_classnote.store import Store
from astrbot_plugin_classnote.tests.test_ics import ALARM, calendar, event
from astrbot_plugin_classnote.tests.test_reminder import (
    binding,
    load_plugin_with_sdk_stubs,
)


class Clock(datetime):
    instant = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


class NativeManager:
    """Minimal public CronJobManager contract; intentionally no private attributes."""

    def __init__(self):
        self.jobs = {}
        self.callbacks = {}
        self.counter = 0
        self.creation_enabled = []

    async def add_basic_job(self, *, handler, **fields):
        self.counter += 1
        self.creation_enabled.append(fields["enabled"])
        job = types.SimpleNamespace(
            job_id=f"native-{self.counter}", run_once=False, last_error=None, **fields
        )
        self.jobs[job.job_id] = job
        self.callbacks[job.job_id] = handler
        return job

    async def update_job(self, job_id, **fields):
        job = self.jobs.get(job_id)
        if job:
            for key, value in fields.items():
                setattr(job, key, value)
        return job

    async def list_jobs(self):
        return list(self.jobs.values())

    async def delete_job(self, job_id):
        self.jobs.pop(job_id, None)
        self.callbacks.pop(job_id, None)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.main = load_plugin_with_sdk_stubs()
        self.manager = NativeManager()
        context = types.SimpleNamespace(
            cron_manager=self.manager, send_message=AsyncMock(return_value=True)
        )
        self.plugin = self.main.ClassNote(
            context, {"use_llm_persona": False, "scheduler_mode": "future_tasks"}
        )
        self.plugin.store = Store(":memory:")
        self.b = self.plugin.store.save(binding())
        self.future = self.plugin.future_tasks
        self.clock = patch("astrbot_plugin_classnote.future_tasks.datetime", Clock)
        self.clock.start()
        self.main_clock = patch.object(self.main, "datetime", Clock)
        self.main_clock.start()
        Clock.instant = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.clock.stop()
        self.main_clock.stop()
        self.plugin.store.close()

    async def test_real_dated_tasks_and_repeated_sync_are_idempotent(self):
        await self.future.sync()
        jobs = await self.manager.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].run_once)
        self.assertEqual(
            datetime.fromisoformat(jobs[0].payload["run_at"]),
            datetime(
                2026,
                10,
                26,
                7,
                40,
                tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"),
            ),
        )
        self.assertEqual(self.manager.creation_enabled, [False])
        await self.future.sync()
        self.assertEqual(self.manager.counter, 1)

    async def test_native_callback_and_polling_share_one_delivery_claim(self):
        await self.future.sync()
        job = (await self.manager.list_jobs())[0]
        Clock.instant = datetime.fromisoformat(job.payload["run_at"])
        await self.future.callback(**job.payload)
        await self.future.callback(**job.payload)
        live = self.plugin.store.get(self.b["id"])
        item = __import__(
            "astrbot_plugin_classnote.core", fromlist=["due_reminders"]
        ).due_reminders(live, Clock.instant)[0]
        self.plugin.config["scheduler_mode"] = "polling"
        await self.plugin._deliver(live, item)
        self.assertEqual(self.plugin.context.send_message.await_count, 1)
        self.assertEqual(
            self.plugin.store.delivery(job.payload["key"])["state"], "sent"
        )

    async def test_early_execution_does_not_send_or_claim(self):
        await self.future.sync()
        job = (await self.manager.list_jobs())[0]
        await self.future.callback(**job.payload)
        self.plugin.context.send_message.assert_not_awaited()
        self.assertIsNone(self.plugin.store.delivery(job.payload["key"]))

    async def test_disabled_job_and_restart_preserve_pause(self):
        await self.future.sync()
        job = (await self.manager.list_jobs())[0]
        await self.manager.update_job(job.job_id, enabled=False)
        await self.future.sync(rebind=True)
        fresh = (await self.manager.list_jobs())[0]
        self.assertNotEqual(fresh.job_id, job.job_id)
        self.assertFalse(fresh.enabled)
        await self.future.close()
        self.assertEqual(await self.manager.list_jobs(), [])
        await self.future.sync(rebind=True)
        self.assertFalse((await self.manager.list_jobs())[0].enabled)

    async def test_manual_delete_is_not_undone_without_explicit_regenerate(self):
        await self.future.sync()
        job = (await self.manager.list_jobs())[0]
        await self.manager.delete_job(job.job_id)
        await self.future.sync()
        self.assertEqual(await self.manager.list_jobs(), [])
        await self.future.sync(rebind=True)
        self.assertEqual(await self.manager.list_jobs(), [])
        await self.future.sync(reset_deleted=True)
        self.assertEqual(len(await self.manager.list_jobs()), 1)

    async def test_pause_and_mode_switch_remove_only_owned_jobs(self):
        await self.future.sync()
        foreign = types.SimpleNamespace(job_id="foreign", payload={"note": "manual"})
        self.manager.jobs["foreign"] = foreign
        self.plugin.config["scheduler_mode"] = "polling"
        await self.future.sync()
        self.assertEqual(await self.manager.list_jobs(), [foreign])
        self.plugin.config["scheduler_mode"] = "future_tasks"
        self.plugin.store.save({**self.b, "enabled": False})
        await self.future.sync()
        self.assertEqual(await self.manager.list_jobs(), [foreign])

    async def test_changed_lead_time_replaces_stale_jobs(self):
        await self.future.sync()
        old = (await self.manager.list_jobs())[0]
        self.plugin.store.save({**self.b, "lead_minutes": [30, 10]})
        await self.future.sync()
        jobs = await self.manager.list_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertNotIn(old.job_id, self.manager.jobs)
        self.assertEqual(sorted(j.payload["lead"] for j in jobs), [10, 30])
        Clock.instant = datetime.fromisoformat(old.payload["run_at"])
        await self.future.callback(**old.payload)
        self.plugin.context.send_message.assert_not_awaited()

    async def test_failed_one_shot_retries_through_a_native_task(self):
        await self.future.sync()
        job = (await self.manager.list_jobs())[0]
        Clock.instant = datetime.fromisoformat(job.payload["run_at"])
        self.plugin.context.send_message.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await self.future.callback(**job.payload)
        await self.manager.delete_job(job.job_id)
        # Model a native one-shot deletion after execution, including failures.
        self.plugin.store.finish(
            job.payload["key"], Clock.instant.timestamp(), "failed", "RuntimeError"
        )
        await self.future.sync()
        retry = (await self.manager.list_jobs())[0]
        self.assertEqual(
            datetime.fromisoformat(retry.payload["run_at"]),
            Clock.instant + timedelta(seconds=30),
        )
        self.assertNotEqual(retry.job_id, job.job_id)

    async def test_horizon_rolls_and_cancelled_dates_never_become_tasks(self):
        self.plugin.store.save({**self.b, "overrides": {"2026-10-26": None}})
        await self.future.sync()
        self.assertEqual(self.future.jobs, [])
        Clock.instant += timedelta(days=7)
        await self.future.sync()
        self.assertEqual(len(self.future.jobs), 1)
        self.assertIn("2026-11-02", self.future.jobs[0].payload["begin"])

    async def test_missing_api_uses_polling_and_recovers_when_manager_appears(self):
        self.plugin.context.cron_manager = None
        await self.future.sync()
        self.assertFalse(self.future.status()["available"])
        self.assertEqual(self.future.status()["mode"], "polling")
        self.assertIn("轮询", self.future.error)
        Clock.instant = datetime(2026, 10, 25, 23, 40, tzinfo=timezone.utc)
        from astrbot_plugin_classnote.core import due_reminders

        item = due_reminders(self.b, Clock.instant)[0]
        await self.plugin._deliver(self.b, item)
        self.plugin.context.send_message.assert_awaited_once()
        self.plugin.context.cron_manager = self.manager
        await self.future.sync()
        self.assertEqual(self.future.status()["mode"], "future_tasks")
        self.assertFalse(self.future.error)
        self.assertTrue(
            all(
                job.payload["key"] != item["key"]
                for job in await self.manager.list_jobs()
            )
        )

    async def test_factory_failure_cleans_disabled_partial_job(self):
        self.manager.update_job = AsyncMock(side_effect=RuntimeError("write failed"))
        with self.assertRaises(RuntimeError):
            await self.future.sync()
        self.assertEqual(await self.manager.list_jobs(), [])


def dashboard_module():
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(error=lambda *_: None)
    web = types.ModuleType("astrbot.api.web")
    web.json_response = lambda data: data
    web.error_response = lambda message, **kwargs: {"error": message, **kwargs}
    web.stream_response = lambda chunks, **kwargs: {
        "text": b"".join(chunks).decode("utf-8"),
        **kwargs,
    }
    web.request = types.SimpleNamespace(query={}, json=AsyncMock(return_value={}))
    with patch.dict(sys.modules, {"astrbot.api": api, "astrbot.api.web": web}):
        return importlib.import_module("astrbot_plugin_classnote.webui")


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.main = load_plugin_with_sdk_stubs()
        self.web = dashboard_module()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.plugin = self.main.ClassNote(
            self.context, {"scheduler_mode": "polling", "use_llm_persona": False}
        )
        self.plugin.store = Store(":memory:")
        self.dashboard = self.web.Dashboard(self.plugin)
        target = binding()
        target["id"] = self.plugin._binding_id(self.dashboard.event(target))
        self.b = self.plugin.store.save(target)
        self.web.request.query = {}
        self.web.request.json = AsyncMock(return_value={})

    async def asyncTearDown(self):
        self.plugin.store.close()

    def payload(self, body):
        self.web.request.json.return_value = body

    async def test_preview_does_not_replace_and_confirm_imports(self):
        body = {
            "binding_id": self.b["id"],
            "kind": "ics",
            "content": calendar(event(extra=ALARM)),
        }
        self.payload(body)
        result = await self.dashboard.import_schedule()
        self.assertEqual(result["preview"]["count"], 1)
        self.assertEqual(self.plugin.store.get(self.b["id"])["revision"], 1)
        self.payload({**body, "confirm": True})
        result = await self.dashboard.import_schedule()
        self.assertIn("已收好", result["message"])
        imported = self.plugin.store.get(result["binding_id"])
        self.assertEqual(imported["schedule"]["kind"], "calendar")

    async def test_invalid_import_preserves_existing_schedule(self):
        self.payload(
            {
                "binding_id": self.b["id"],
                "kind": "ics",
                "content": "bad",
                "confirm": True,
            }
        )
        with self.assertRaises(TimetableError):
            await self.dashboard.import_schedule()
        self.assertEqual(self.plugin.store.get(self.b["id"])["revision"], 1)

    async def test_new_import_rejects_unknown_destination(self):
        self.dashboard.target_list = AsyncMock(return_value=[])
        self.payload(
            {
                "umo": "somewhere:FriendMessage:999",
                "sender": "999",
                "kind": "ics",
                "content": calendar(event()),
                "confirm": True,
            }
        )
        with self.assertRaises(TimetableError):
            await self.dashboard.import_schedule()
        self.assertEqual(len(self.plugin.store.all()), 1)

    async def test_group_import_obeys_global_group_setting(self):
        self.b = self.plugin.store.save({**self.b, "group": True})
        self.payload({"binding_id": self.b["id"], "content": calendar(event())})
        with self.assertRaises(TimetableError):
            await self.dashboard.import_schedule()

    async def test_configuration_is_atomic_when_monday_is_invalid(self):
        self.payload(
            {
                "binding_id": self.b["id"],
                "action": "configure",
                "lead_source": "custom",
                "lead_minutes": "30,10",
                "enabled": False,
                "monday": "2026-10-27",
            }
        )
        with self.assertRaises(TimetableError):
            await self.dashboard.action()
        self.assertEqual(self.plugin.store.get(self.b["id"]), self.b)

    async def test_configure_updates_both_advance_time_and_switch(self):
        self.payload(
            {
                "binding_id": self.b["id"],
                "action": "configure",
                "lead_source": "custom",
                "lead_minutes": "30,10",
                "enabled": False,
                "monday": "2026-10-26",
            }
        )
        await self.dashboard.action()
        live = self.plugin.store.get(self.b["id"])
        self.assertEqual(live["lead_minutes"], [30, 10])
        self.assertFalse(live["enabled"])
        self.assertEqual(live["schedule"]["monday"], "2026-10-26")

    async def test_delete_requires_explicit_confirmation(self):
        self.payload({"binding_id": self.b["id"], "action": "删除"})
        with self.assertRaises(TimetableError):
            await self.dashboard.action()
        self.assertIsNotNone(self.plugin.store.get(self.b["id"]))
        self.payload({"binding_id": self.b["id"], "action": "删除", "confirm": True})
        await self.dashboard.action()
        self.assertIsNone(self.plugin.store.get(self.b["id"]))

    async def test_settings_reject_invalid_types_and_unknown_keys(self):
        for body in (
            {"enabled": "false"},
            {"timezone": "No/SuchZone"},
            {"catchup_seconds": 10},
            {"token": "secret"},
            {"scheduler_mode": "unknown"},
        ):
            self.payload(body)
            with self.subTest(body=body), self.assertRaises((ValueError, KeyError)):
                await self.dashboard.settings()
        self.assertEqual(self.plugin.config["scheduler_mode"], "polling")

    async def test_export_can_be_reimported_with_real_overrides_and_alarms(self):
        self.plugin.store.save(
            {
                **self.b,
                "custom_lead": True,
                "lead_minutes": [30, 10],
                "overrides": {"2026-09-07": None, "2026-09-12": "2026-09-07"},
            }
        )
        self.web.request.query = {"binding": self.b["id"]}
        exported = await self.dashboard.export()
        imported = parse_calendar(exported["text"])
        self.assertNotIn("2026-09-07", imported["dates"])
        self.assertIn("2026-09-12", imported["dates"])
        self.assertEqual(imported["courses"][0]["alarm_minutes"], [30, 10])
        self.assertNotIn(self.b["umo"], exported["text"])

    async def test_snapshot_has_seven_days_and_no_credentials(self):
        self.plugin.config["token"] = "never-expose-this"
        self.web.request.query = {"binding": self.b["id"], "start": "2026-09-07"}
        data = await self.dashboard.state()
        self.assertEqual(len(data["selected"]["days"]), 7)
        self.assertEqual(data["selected"]["days"][0]["courses"][0]["name"], "大学化学")
        self.assertNotIn("token", data["settings"])
        self.assertNotIn("epoch", data["selected"])

    async def test_registered_routes_have_plugin_prefix_and_unready_guard(self):
        self.context.register_web_api = lambda *args: registrations.append(args)
        registrations = []
        self.dashboard.register()
        self.assertEqual(len(registrations), 7)
        self.assertTrue(
            all(
                row[0].startswith("/astrbot_plugin_classnote/") for row in registrations
            )
        )
        store = self.plugin.store
        self.plugin.store = None
        result = await registrations[0][1]()
        self.assertEqual(result["status_code"], 503)
        self.plugin.store = store

    async def test_missing_web_sdk_does_not_break_core_plugin(self):
        self.context.register_web_api = lambda *args: None
        with patch.dict(sys.modules, {"astrbot_plugin_classnote.webui": None}):
            self.plugin._setup_dashboard()
        self.assertIsNone(self.plugin.dashboard)
        self.assertIn("QQ", self.plugin.dashboard_error)
        self.assertIsNotNone(self.plugin.store.get(self.b["id"]))

    async def test_failed_registration_rolls_back_only_own_routes(self):
        foreign = ("/foreign", lambda: None, ["GET"], "Foreign")
        self.context.registered_web_apis = [foreign]

        def register(*args):
            self.context.registered_web_apis.append(args)
            if len(self.context.registered_web_apis) == 3:
                raise RuntimeError("registration failed")

        self.context.register_web_api = register
        with patch.dict(sys.modules, {"astrbot_plugin_classnote.webui": self.web}):
            self.plugin._setup_dashboard()
        self.assertIsNone(self.plugin.dashboard)
        self.assertEqual(self.context.registered_web_apis, [foreign])

    async def test_dashboard_unload_removes_routes_and_accepts_context_only_constructor(
        self,
    ):
        plugin = self.main.ClassNote(self.context)
        self.assertEqual(plugin.config, {})
        self.context.registered_web_apis = []
        self.context.register_web_api = lambda *args: (
            self.context.registered_web_apis.append(args)
        )
        with patch.dict(sys.modules, {"astrbot_plugin_classnote.webui": self.web}):
            self.plugin._setup_dashboard()
        self.assertEqual(len(self.context.registered_web_apis), 7)
        self.plugin.dashboard.close()
        self.assertEqual(self.context.registered_web_apis, [])

    async def test_foreign_task_cannot_be_modified(self):
        manager = NativeManager()
        manager.jobs["foreign"] = types.SimpleNamespace(job_id="foreign", payload={})
        self.plugin.context.cron_manager = manager
        self.payload({"operation": "toggle", "id": "foreign", "enabled": False})
        with self.assertRaises(TimetableError):
            await self.dashboard.tasks()
        self.assertIn("foreign", manager.jobs)

    async def test_target_query_does_not_load_chat_history(self):
        get = AsyncMock(
            return_value=(
                [
                    types.SimpleNamespace(
                        user_id="qq1:FriendMessage:12345", title="我的会话"
                    )
                ],
                1,
            )
        )
        self.context.conversation_manager = types.SimpleNamespace(
            get_filtered_conversations=get
        )
        self.context.get_platform_inst = lambda _: types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(name="aiocqhttp")
        )
        targets = await self.dashboard.target_list()
        self.assertEqual(targets[0]["umo"], self.b["umo"])
        self.assertFalse(get.call_args.kwargs["include_history"])


if __name__ == "__main__":
    unittest.main()
