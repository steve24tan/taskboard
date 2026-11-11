# TaskBoard

今天的日常 / 这周的周本跑完没有?一页看完。

自动化脚本(MAA、三月七小助手…)各跑各的,TaskBoard 只**读**它们自己留下的日志和设定档,
按游戏服务器的重置时间(预设 UTC+8 04:00,周本周一 04:00)算出每个任务这一期的状态:

| 状态 | 意思 |
|---|---|
| ✓ 已完成 | 这一期有一次成功 |
| ~ 部分完成 | 跑完了,但有子任务没做完(例:三月七「每日实训未完成」) |
| ✕ 失败 | 只有失败记录 |
| · 未跑 | 这一期还没有记录 |

同一期里**手动标记**优先;否则取最好的结果(重试成功会盖掉前一次失败)。

## 跑起来

只需要 Python 3.10+,不用装任何套件。

```
copy config.example.json config.json   # 改里面的路径
python taskboard.py --once             # 先扫一次,看日志读不读得到
python taskboard.py                    # 开 dashboard: http://localhost:8765
```

或者双击 `start_board.bat`。同一个局域网的手机用 `http://<那台电脑的IP>:8765` 打开
(第一次 Windows 防火墙会问,选「私人网络」)。

资料存在 `data/board.db`(SQLite);删掉它会从日志重新扫回最近 14 天。

## 数据来源 (collectors)

| type | 读什么 | 判断 |
|---|---|---|
| `maa_wrapper` | `maa-tools/logs/YYYY-MM-DD.log`(run_maa.py 写的) | `跑 MAA: <命令>` 用 `profiles` 对应到任务;`OK 完成` = 完成,`X ...` = 失败 |
| `march7th` | 三月七目录的 `logs/YYYY-MM-DD.log` 和 `config.yaml` | 日志:`开始运行` … `停止运行` 一轮,中间有 `发生错误` = 失败、`清体力未完成` 之类 = 部分完成;周本:`config.yaml` 的 `echo_of_war_timestamp` 等时间戳(三月七完成时自己写的) |

| `maa_autorun` | `run_dailies.ps1` 写的 `autorun_YYYYMMDD.log`,行首 `[日期 时间] <账号> :` | `accounts` 把账号(TW / Global …)对应到任务;`all daily tasks completed` = 完成,`these failed: …` = 部分完成,超时 / `NOT stamped done` = 失败 |
| `csv_dates` | 任意 CSV(例:`episode_log.csv`) | `date_column` 有日期的每一行 = 一次完成(只有日期时算当天 12:00) |
| `csv_due` | 同上 | 日期过了 `due_days` 天,`require_columns` 还有空格 = 失败(列出 `key_column`);没有到期未补的 = 完成。每次扫描记一笔快照,任务要配 `"resolve": "latest"` |
| `github_runs` | `gh run list`(GitHub CLI,用 `gh auth login` 的账号,私有 repo 也行) | 某个 workflow 的 `success` = 完成,`failure` / 超时 = 失败;取消和没跑完的不算。最多每 `every_seconds`(默认 3600)叫一次 gh |
| `http_ping` | 一个 URL | 不产生任务,只在「数据来源」显示服务在不在线(有任何 HTTP 回应就算在线) |

三月七日志标记沿用 [sheinlee/daily-hub](https://github.com/sheinlee/daily-hub)(MIT)对 v2026.7.26 的验证。
三月七改版后标记可能变,`--once` 和 dashboard 底下「数据来源」会显示读了几个日志、缺哪个 key。
任何 collector 加 `"enabled": false` 就先不扫。

## 任务选项

| 键 | 默认 | 说明 |
|---|---|---|
| `cadence` | `daily` | `daily` / `weekly`(周一重置)/ `monthly` |
| `reset_hour` | 群组或全局的 `reset_hour` | 几点重置 |
| `target` | `1` | 这一期要几次「完成」。不够就是部分完成,显示 `1/2` |
| `resolve` | `best` | `best` = 这一期最好的一次结果;`latest` = 最新那一次(给 `csv_due` 这种快照用) |

## 其他东西也想上板

在 `config.json` 加一个任务,然后任何脚本都可以 POST:

```
curl -X POST http://localhost:8765/api/event ^
  -H "Content-Type: application/json" -H "X-Board-Token: <config 的 token>" ^
  -d "{\"task\":\"study.review\",\"status\":\"done\",\"detail\":\"12 张卡\",\"key\":\"2026-11-10\"}"
```

- `status`:`done` / `partial` / `fail`
- `key`:同一个 key 只记一次(重送不会重复)
- `token` 留空 = 不检查(只在家里局域网用时)

## API

| | |
|---|---|
| `GET /api/status` | 全部任务目前状态 + 历史 + 数据来源健康 |
| `POST /api/scan` | 立刻重扫 |
| `POST /api/tick` `{"task","done":true/false}` | 手动标记 / 取消 |
| `POST /api/event` | 见上 |

## 测试

```
python -m unittest discover -s tests
```
