"""Load the checkout with an installed, unmodified AstrBot (no SDK stubs).

Run in a separate process: python tests/runtime_integration.py
Pass --archive /path/to/plugin.zip to test the real upload installer, dependency
installation and replacement of an already-loaded plugin, retaining its data.
External QQ delivery is replaced with AsyncMock; the loader, database and native
scheduler are real. A temporary ASTRBOT_ROOT prevents touching existing bot data.
"""

import argparse
import asyncio
import importlib
import inspect
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


async def verify(archive=None, previous_archive=None):
    from astrbot.core import astrbot_config, db_helper
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.cron.manager import CronJobManager
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_manager import PluginManager

    await db_helper.initialize()
    astrbot_config["disable_metrics"] = True
    cron = CronJobManager(db_helper)
    required = {
        name: None
        for name, parameter in inspect.signature(Context).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    required.update(
        event_queue=asyncio.Queue(),
        config=astrbot_config,
        db=db_helper,
        provider_manager=SimpleNamespace(),
        platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=ConversationManager(db_helper),
        astrbot_config_mgr=SimpleNamespace(get_conf=lambda *a, **kw: astrbot_config),
        cron_manager=cron,
    )
    context = Context(**required)
    context.send_message = AsyncMock(return_value=True)
    manager = PluginManager(context, astrbot_config)
    plugin = None
    archive_bytes = archive.read_bytes() if archive else None
    try:
        if previous_archive:
            await manager.install_plugin_from_file(str(previous_archive))
        if archive:
            await manager.install_plugin_from_file(str(archive))
        else:
            success, error = await manager.load(
                specified_dir_name="astrbot_plugin_classnote"
            )
            assert success, error
        success, error = await manager.reload("astrbot_plugin_classnote")
        assert success, error
        plugin = next(
            meta.star_cls
            for meta in star_registry
            if meta.name == "astrbot_plugin_classnote" and meta.star_cls
        )
        assert plugin.store is not None and not plugin.http.closed
        try:
            importlib.import_module("astrbot.api.web")
            modern_web = True
        except ImportError:
            modern_web = False
        assert bool(plugin.dashboard) == modern_web
        assert len(context.registered_web_apis) == (7 if modern_web else 0)
        plugin.config["use_llm_persona"] = False
        module = plugin.__class__.__module__.rsplit(".", 1)[0]
        parse_calendar = importlib.import_module(module + ".ics").parse_calendar
        begin = (datetime.now(timezone.utc) + timedelta(minutes=20)).replace(
            second=0, microsecond=0
        )
        end = begin + timedelta(minutes=45)
        schedule = parse_calendar(
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:runtime-test\n"
            f"DTSTART:{begin:%Y%m%dT%H%M%SZ}\nDTEND:{end:%Y%m%dT%H%M%SZ}\n"
            "SUMMARY:加载回归课程\nEND:VEVENT\nEND:VCALENDAR"
        )
        plugin.store.save(
            dict(
                id="runtime-binding",
                umo="qq:FriendMessage:runtime",
                sender="runtime",
                nickname="测试",
                platform="aiocqhttp",
                group=False,
                timezone="Asia/Shanghai",
                lead_minutes=[20],
                enabled=True,
                overrides={},
                schedule=schedule,
            )
        )
        await plugin.future_tasks.sync()
        jobs = await cron.list_jobs()
        assert len(jobs) == 1 and jobs[0].run_once and jobs[0].enabled
        job_id = jobs[0].job_id
        await cron.update_job(job_id, enabled=False)
        plugin.config["task_requirement_template"] = (
            "新设置：提醒 {nickname} 去上 {course}，请带水杯。"
        )
        await plugin.future_tasks.sync()
        updated = (await cron.list_jobs())[0]
        assert updated.job_id == job_id and not updated.enabled
        assert "新设置：提醒 测试 去上 加载回归课程" in updated.description
        # Exercise the native task editor's real public service where available.
        try:
            from astrbot.dashboard.services.cron_service import CronService
        except ImportError:
            await cron.update_job(
                job_id, description="单条需求：请轻声提醒，带上笔记本。"
            )
        else:
            native = await CronService(context).update_job(
                job_id, {"description": "单条需求：请轻声提醒，带上笔记本。"}
            )
            assert native["note"] == "单条需求：请轻声提醒，带上笔记本。"
        await plugin.future_tasks.sync()
        plugin.config["task_requirement_template"] = "全局再次修改 {course}"
        await plugin.future_tasks.sync()
        assert (await cron.list_jobs())[
            0
        ].description == "单条需求：请轻声提醒，带上笔记本。"
        await cron.update_job(job_id, enabled=True)
        await plugin.future_tasks.callback(**jobs[0].payload)
        await plugin.future_tasks.callback(**jobs[0].payload)
        context.send_message.assert_awaited_once()
        import base64
        from io import BytesIO

        from astrbot.api.message_components import Image
        from PIL import Image as PILImage

        chain = context.send_message.call_args.args[1].chain
        assert len(chain) == 2 and isinstance(chain[1], Image)
        data = base64.b64decode(chain[1].file.removeprefix("base64://"))
        with PILImage.open(BytesIO(data)) as card:
            assert card.size == (1000, 430) and card.format == "PNG"
            card.verify()
        assert plugin.store.delivery(jobs[0].payload["key"])["state"] == "sent"
        await plugin.future_tasks.sync()
        assert not await cron.list_jobs()
        if archive:
            previous = plugin
            archive.write_bytes(archive_bytes)
            await manager.install_plugin_from_file(str(archive))
            plugin = next(
                meta.star_cls
                for meta in star_registry
                if meta.name == "astrbot_plugin_classnote" and meta.star_cls
            )
            assert previous.store is None and previous.http.closed
            assert plugin.store.get("runtime-binding") is not None
            assert plugin.store.delivery(jobs[0].payload["key"])["state"] == "sent"
            assert len(context.registered_web_apis) == (7 if modern_web else 0)
            assert not await cron.list_jobs()
        await plugin.terminate()
        assert not context.registered_web_apis
        assert plugin.store is None and plugin.http.closed
        print(
            f"PASS AstrBot {version('astrbot')}: archive={bool(archive)}, reload, "
            f"web={modern_web}, native task, delivery dedup, replacement, unload"
        )
    finally:
        if plugin and plugin.store:
            await plugin.terminate()
        await cron.shutdown()
        await db_helper.engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--previous-archive", type=Path)
    args = parser.parse_args()
    if args.previous_archive and not args.archive:
        parser.error("--previous-archive requires --archive")
    source = Path(__file__).resolve().parents[1]
    archive = args.archive.resolve() if args.archive else None
    previous = args.previous_archive.resolve() if args.previous_archive else None
    with tempfile.TemporaryDirectory(prefix="classnote-runtime-") as directory:
        root = Path(directory)
        os.environ["ASTRBOT_ROOT"] = directory
        for relative in ("data/config", "data/plugins"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        if archive:
            shutil.copy2(archive, root / "plugin.zip")
            if previous:
                shutil.copy2(previous, root / "previous.zip")
        else:
            shutil.copytree(
                source,
                root / "data/plugins/astrbot_plugin_classnote",
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
            )
        sys.path.insert(0, directory)
        os.chdir(root)
        asyncio.run(
            verify(
                root / "plugin.zip" if archive else None,
                root / "previous.zip" if previous else None,
            )
        )


if __name__ == "__main__":
    main()
