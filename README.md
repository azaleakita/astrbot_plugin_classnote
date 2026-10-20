<p align="center"><img src="docs/assets/hero.svg" alt="课笺 ClassNote · 让每一次上课，都有一句温柔的提醒" width="100%" /></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.2.0-a58bc7?style=flat-square" alt="version 0.2.0" />
  <img src="https://img.shields.io/badge/AstrBot-%E2%89%A54.19.6-75629a?style=flat-square" alt="AstrBot >=4.19.6" />
  <img src="https://img.shields.io/badge/Python-3.11%2B-5a718b?style=flat-square" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/iCalendar-ICS-b38d9f?style=flat-square" alt="iCalendar ICS" />
  <img src="https://img.shields.io/badge/license-MIT-8b8198?style=flat-square" alt="MIT license" />
</p>

<p align="center"><b>把课程记在笺上，把提醒留给熟悉的声音。</b><br />WakeUp 课表导入 · QQ 主动提醒 · 单双周与调课 · 当前会话人格</p>

<p align="center"><a href="#快速开始">快速开始</a> · <a href="#导入你的课表">导入课表</a> · <a href="#常用指令">指令一览</a> · <a href="docs/INSTALL.md">安装与升级</a> · <a href="DESIGN.md">开发设计</a></p>

---

课笺是一个 AstrBot 课程表提醒插件。你可以把 WakeUp 导出的 `.ics` 日历文件直接发给 bot，也可以导入兼容的分享口令。它按照每一次真实上课安排计算提醒，并读取当前 QQ 会话的人格和模型，让 bot 用熟悉的语气喊你去上课。

<p align="center"><img src="docs/assets/reminder-preview.svg" alt="单双周排课与QQ提醒示意，所有课程信息均为虚构" width="100%" /></p>

## 这张课笺能记住什么

| 能力 | 你会得到的体验 |
| --- | --- |
| WakeUp 日历文件 | 直接发送 `.ics` 或 `.ical` 文件即可导入，不依赖分享口令继续有效 |
| 每周不同的课程 | 支持重复规则、单双周、非连续日期和例外日期，按实际课次提醒 |
| 日历中的调课 | 识别 `EXDATE`、`RDATE`、`RECURRENCE-ID` 与取消状态 |
| bot 自己的语气 | 每次提醒读取当前会话人格与模型，课程、时间、地点由程序保留 |
| 你的提前量 | 可沿用日历自带提醒，也可设置提前 30、10 分钟分别提醒 |
| 运行恢复 | SQLite 保存课表及投递记录，支持去重、有限重试和短时补发 |

## 快速开始

**环境：AstrBot >=4.19.6、<5；Python >=3.11；已接入能够主动发送消息的 QQ 适配器。**

1. 在 AstrBot 插件管理中从 `https://github.com/azaleakita/astrbot_plugin_classnote` 安装，或将仓库文件放进 `data/plugins/astrbot_plugin_classnote/`。
2. 重载插件，确认自己的 QQ 账号已加入 AstrBot 管理员。默认只开放管理员私聊使用。
3. 在 WakeUp 中选择“导出为日历文件”，将导出的 `.ics` **作为文件附件**发送给 bot。

然后用下面两条指令核对：

```text
/课表 下次
/课表 测试
```

“下次”展示接下来的计划提醒；“测试”使用正式提醒的主动投递和人格接口，不消耗正式提醒记录。

## 导入你的课表

### 推荐：WakeUp 日历文件

WakeUp 主界面右上角的导出入口 → **导出为日历文件** → 将 `.ics` 文件发送给 bot。

默认开启直接附件导入。若关闭了 `auto_import_ics`，则先发送 `/课表 导入日历`，再在 **2 分钟内**发送文件。也支持在带附件的消息中使用这条指令，或引用能够提供文件消息段的消息执行导入。待导入状态不会跨会话共享。

日历中的每次课程保留实际日期和起止时间。有限 `RRULE`、`RDATE`、`EXDATE`、单次改期和取消会在导入时展开；UTF-8 中文、折行、地点、UTC 与 `TZID` 时区由日历解析库处理。

WakeUp Android/鸿蒙版导出日历默认带提前 20 分钟提醒。新订阅优先读取日历里的相对开始时间提醒；没有可用提醒时使用插件默认值。手动设置的提前量优先级更高：

```text
/课表 提前 30,10
/课表 提前 日历
```

第一条覆盖为提前 30 和 10 分钟，第二条恢复日历设置。绝对时间闹钟、相对结束时间闹钟不会转换为上课提醒。

> 日历导入是一次快照。WakeUp 中之后新增或修改课程，需要重新导出再发送；插件不会自行读取你的教务账号。

### 兼容：分享文本与备份文本

```text
/课表 导入 完整的WakeUp分享文本或口令
/课表 导入文本 原始WakeUp备份文本或兼容JSON
```

分享导入适配经典 `share_schedule/get` 协议，不保证新版鉴权或加密分享兼容。遇到协议错误时，可改用日历文件。备份文本保留教学周、单双周、非连续周次与节次作息。

所有导入都会先完整校验再替换课表。解析失败时，原课表和提醒设置仍保留。

## 常用指令

| 指令 | 作用 |
| --- | --- |
| `/课表 导入日历` | 等待或读取 WakeUp `.ics` 附件 |
| `/课表 今日`、`/课表 明日`、`/课表 本周` | 查询实际课程 |
| `/课表 下次` | 查看接下来的 5 次计划提醒 |
| `/课表 状态` | 查看日期范围、提醒设置及投递状态 |
| `/课表 提前 20` | 使用自己的提前时间，单位为分钟 |
| `/课表 提前 30,10` | 最多设置 3 次提前提醒 |
| `/课表 提前 日历` | 恢复使用 ICS 自带的相对提醒 |
| `/课表 停课 2026-10-21` | 停止当天全部课程提醒 |
| `/课表 调课 2026-10-24 2026-10-19` | 24 日改上 19 日的课程，替换目标日安排 |
| `/课表 恢复 2026-10-24` | 移除当天的停课或调课覆盖 |
| `/课表 开学 2026-09-07` | 校准分享/备份课表的第一教学周周一 |
| `/课表 暂停`、`/课表 开启` | 保留课表，暂停或恢复提醒 |
| `/课表 测试` | 主动投递下一门课程的测试提醒 |
| `/课表 删除` | 删除你在当前会话的课表及投递记录 |

**调课不自动取消原日期。** 若周六补周一课、周一放假，分别设置“调课”和“停课”。重新导入会保留提前量、暂停状态及手动日期覆盖；新学期想完全重置时，先删除再导入。

日历按真实日期排课，不通过“开学”指令整体平移。该指令只用于包含教学周规则的分享/备份课表。

## 提醒，仍然是你的 bot

课笺每次提醒都读取**目标会话**生效的人格与模型。可用时复用 AstrBot 主 Agent 的统一人格解析器，兼容会话规则强制人格、对话人格及默认人格选择。

人格提示及少量人格预设对话用于生成简短开头；准确的课程名、日期、起止时间和地点由程序追加。修改主人格后，后续提醒随之使用新设定。

模型异常或超时时，使用 `fallback_opener` 继续发送详情；建议把备用开头也设成 bot 的风格。每个待投递提醒通常调用一次模型，重试可能再次调用。关闭 `use_llm_persona` 可使用固定开头。日常聊天历史不会发送给模型，因此其他插件追加的记忆或动态提示不一定被继承。

## QQ、权限与数据

- OneBot v11 / NapCat，以及具备主动消息能力的 QQ 官方适配器，可通过 AstrBot 接口投递；文件接收与主动权限应在自己的环境中验证。
- 默认管理员私聊可用。开放 `allow_group` 后，在群内导入即订阅该群提醒，投递时只 @ 导入者。
- 每个“会话 + 用户”单独保存课表。私聊课表不会自动发到群；在多处订阅可能分别收到提醒。
- 数据保存在 `data/plugin_data/astrbot_plugin_classnote/timetables.sqlite3`。仓库只包含虚构课表示例。
- AstrBot 和 QQ 适配器需要持续在线。默认只补发 5 分钟窗口内、课程尚未开始的提醒；失败最多尝试 3 次。
- 轮询和模型调用存在延迟；极端崩溃窗口可能重复，不承诺 QQ 端严格恰好一次或已读。

## 从旧版升级

旧插件名为 `astrbot_plugin_wakeup_reminder`。**先停用旧插件，再安装课笺**，避免两份插件同时提醒。

首次启动时，若新数据库不存在，会通过 SQLite 备份接口复制旧数据；旧文件保留不动，订阅、提前量和投递记录随之迁移。旧版配置需在新插件设置中核对，群聊默认仍关闭。详见 [安装与升级](docs/INSTALL.md)。

## 兼容范围与测试

文件上限 2 MiB，原始事件最多 2000 条，展开最多 10000 课次，整体跨度最多 550 天。只接收带精确起止时间的有限课程；全天事件、无限重复、小时/分钟级重复、无效时间和未知时区会明确拒绝。主要面向 `Asia/Shanghai`，夏令时切换未专项验证。

当前 **52 项离线测试通过**，覆盖重复规则、例外、时区、附件读取到入库、日历闹钟与手动提前量、旧数据迁移，以及原有调度、人设、重试和去重。SDK 契约测试使用替身；尚未在用户真实 WakeUp 文件和 QQ 实例上完成联调。

```bash
# 在插件目录的父目录运行
python -m pip install -r astrbot_plugin_classnote/requirements.txt
python -m unittest discover -s astrbot_plugin_classnote/tests -v
```

附有 [虚构日历样例](examples/wakeup-demo.ics)、[变更记录](CHANGELOG.md)、[设计说明](DESIGN.md) 和自动测试工作流。

## 致谢与许可

基于 [AstrBot 官方插件文档](https://docs.astrbot.app/dev/star/plugin-new.html)；日历能力使用 [icalendar](https://icalendar.readthedocs.io/) 和 [recurring-ical-events](https://recurring-ical-events.readthedocs.io/)。WakeUp 导出方式参见 [官方使用文档](https://www.wakeup.fun/doc/export_schedule.html)。

经典分享字段与五段备份协议参考 [koishi-plugin-course-schedule](https://github.com/VincentZyuApps/koishi-plugin-course-schedule) 作者公开维护的实现说明。本插件独立编写，未复制其源代码。

[MIT License](LICENSE) · CREAM / azaleakita
