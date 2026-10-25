"""Load the checkout with an installed, unmodified AstrBot (no SDK stubs).

Run in a separate process: python tests/runtime_integration.py
External QQ delivery is replaced with AsyncMock; the loader, database and native
scheduler are real. A temporary ASTRBOT_ROOT prevents touching existing bot data.
"""

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


async def verify():
    from astrbot.core import astrbot_config, db_helper
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.cron.manager import CronJobManager
    from astrbot.core.star.context import Context
    from astrbot.core.star.star import star_registry
    from astrbot.core.star.star_manager import PluginManager

    await db_helper.initialize()
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
    try:
        success, error = await manager.load(
            specified_dir_name="astrbot_plugin_classnote"
        )
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
        await plugin.future_tasks.callback(**jobs[0].payload)
        await plugin.future_tasks.callback(**jobs[0].payload)
        context.send_message.assert_awaited_once()
        assert plugin.store.delivery(jobs[0].payload["key"])["state"] == "sent"
        await plugin.future_tasks.sync()
        assert not await cron.list_jobs()
        await plugin.terminate()
        assert not context.registered_web_apis
        assert plugin.store is None and plugin.http.closed
        print(
            f"PASS AstrBot {version('astrbot')}: real loader, web={modern_web}, native task, delivery dedup, unload"
        )
    finally:
        if plugin and plugin.store:
            await plugin.terminate()
        await cron.shutdown()
        await db_helper.engine.dispose()


def main():
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="classnote-runtime-") as directory:
        root = Path(directory)
        os.environ["ASTRBOT_ROOT"] = directory
        for relative in ("data/config", "data/plugins"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            root / "data/plugins/astrbot_plugin_classnote",
            ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
        )
        sys.path.insert(0, directory)
        os.chdir(root)
        asyncio.run(verify())


if __name__ == "__main__":
    main()
