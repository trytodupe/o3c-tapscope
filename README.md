# Rapid Trigger Capture

SayoDevice O3C 的磁轴深度采集、主机按键同钟录制与 osu! replay 对齐。**仅支持 Windows**；采集器只发送已确认的只读轮询帧，不写配置、不校准、不更新固件。第三方组件（含修改过的 replayviewer-js）见 `THIRD_PARTY.md`。

## Quick start

需要：Windows、[uv](https://docs.astral.sh/uv/)、一块 SayoDevice O3C、osu!stable，以及可选的 [tosu](https://github.com/Kanawanagasaki/tosu)（装在 `http://127.0.0.1:24050`，用来给对齐提供粗锚点）。

```powershell
git clone <repo> ; cd rapid-trigger
uv sync
uv run python tools/studio.py          # 打开 http://127.0.0.1:8770/
```

第一次打开后在页面 **settings** 里填 **osu! folder**（如 `D:\osu!`，Songs / Replays / Skins 都从它派生）、每个键的名字与 **RT 区间**（low/high，mm），保存后**重启 studio** 生效。之后点 **Start capture** 录制，导出一局的 `.osr`，studio 会自动 stage 并在 **aligned replays** 里给出链接。设置存在 `output/settings.json`（不入库）；`--osu` / `--port` / `--skin` 等命令行参数可临时覆盖。osu!lazer 暂不支持。

## 磁轴深度采集（协议已确认）

官方监视页不是被动广播，它按约 20 Hz 主动轮询 `Col03`；采集器改成**逐键读 `cmd 0x14`**（每个键的实时电平在 `payload[8]`），三个键合计约 650 Hz。**注意**：`0x15` 广播只会带上被逐键读取刷新过的通道——只轮询 `0x15` 时中间那个键（X）会恒为 0，所以采集器、实时图都走逐键读。

| 项 | 值 |
| --- | --- |
| 通道 | `Col03`，usage page `0xFF12`，report id `0x22`，1023 字节 payload |
| 请求 | `12 3c 12 05 00 15 <index> 00 …`（`kind=0x05` 读实时值） |
| 响应 | `12 <counter u16> 12 07 00 15 <index> <lv0> <lv1> <lv2> …` |
| 电平 | `lv0/lv1/lv2` 是三个磁轴键各自的实时位置，同一个 raw 0..79 刻度；一次应答返回全部键 |
| 按键 | 通道 0 ↔ Z（vk 90 / scan 0x2C）、通道 1 ↔ X（88 / 0x2D）、通道 2 ↔ C（67 / 0x2E）；旋钮没有模拟通道 |
| 速率 | 逐键读 `0x14`，三键合计约 640–650 Hz（单次往返约 0.49 ms；`0x15` 一次能返三字节，但只刷一个通道） |
| 单位 | raw 0..80；线性 50 μm/raw（raw 79 = 3.95 mm、raw 80 = 4.00 mm，见「标定」），纵轴直接用 mm |

```powershell
uv sync
uv run python tools/tap_capture.py --out tap.jsonl      # 电平 + 主机按键，推荐
uv run python tools/tap_capture.py --out tap.jsonl --state-url   # 再加一路游戏状态（见下）
uv run python tools/hall_capture.py --out hall.jsonl    # 只要电平
uv run python tools/probe_capture.py --out probe.jsonl  # 轮询多条命令，协议排查用
```

默认 `--window-min 10`：录制线程只保留最后 10 分钟的记录（`0` 表示不限）。窗口按记录自带的 `host_ns` 切，文件头（`metadata`）和无时间戳的 `note` 始终保留；`end` 记录里会带上 `window_s`、`dropped_window`、`dropped_overflow`。写盘不是逐条 flush：记录先进队列，由单独的写线程按「每 256 条或每 100 ms」批量落盘，所以低层键盘钩子里的回调不会做文件 I/O（一次撞上杀软过滤驱动的 flush 会让 Windows 静默摘掉钩子，并抖掉 host 时间戳）。

`tap_capture.py` 把两条流写在同一个单调时钟上：

```text
{"type": "levels",   "host_ns": .., "levels": [lv0, lv1, lv2]}
{"type": "keyboard", "host_ns": .., "vk": .., "scan": .., "down": .., "injected": ..}
```

设备的键盘 collection（`Col04`/`Col05`）被 Windows 键盘类驱动占用，hidapi 读不到，所以主机侧按键只能来自 `WH_KEYBOARD_LL` 钩子。判断「这一次按压有没有真的动磁轴」必须看 `levels`：主机出现了 Z/X 而三个通道都没动，说明那是主键盘打的，不是手柄。

## 原始报告采集

`tools/hid_capture.py` 只记录原始厂商报告、不解析磁轴字段，用于确认接口、固件行为或协议边界。`--send-hex` 必须与明确的 `--index` 或 `--path` 一起使用，并且只允许发送已确认只读语义的帧。

```powershell
uv run python tools/hid_capture.py --list
uv run python tools/hid_capture.py --vid 0x8089 --pid 0x0009 --out capture-native.jsonl
```

O3C 枚举出的厂商通道：`Col01`（usage page `0xFF00`）、`Col02`（`0xFF11`，report `0x21`，1 Hz 心跳，不含深度）、`Col03`（`0xFF12`，report `0x22`，磁轴电平通道）。`report_id` 是原始报告的第一个字节，native 输出的 `hex` 包含 report ID；`host_ns` 是高精度单调时钟。按 Ctrl+C 正常结束会写入 `end` 记录。

Web HID 页面（`web/`）仍可用于快速确认浏览器能看到哪些接口，但不再是正式采集路径：它读不到被浏览器保护的键盘接口，报告时间也只代表浏览器收到事件的时刻。

```powershell
python -m http.server 8765 --bind 127.0.0.1 --directory web
```

## osu! replay 对齐

按键序列自动对齐：手柄按键和 replay 的 K1/K2 位翻转来自同一次物理按压，所以扫描 offset、取匹配数最多且残差最小的那个即可。正确对齐表现为接近 1:1 的匹配和亚毫秒残差，`assign` 报告会给出 vk ↔ K1/K2 的对应关系。

```powershell
uv run python tools/osu_align.py tap.jsonl --replay "play.osr" --keys 90,88 --out output/session
uv run python tools/plot_depth.py output/session/aligned.jsonl --out depth-aligned.html
```

`--keys` 是手柄两个键的 vk（默认 `90,88`，即 Z / X）；`--tolerance-ms` 是匹配容差；`--level-threshold 6,4,8` 是各键的激活阈值（C 键静息就漂到 1–4，不能全局取同一个值）；`--gap-ms` 是判为双击的「松开 → 再按下」间隔。输出目录含 `aligned.jsonl`（深度 + 主机按键 + replay 边线，可整体交给 `plot_depth.py`）和 `alignment.json`。

支持经典二进制 `.osr` 的 LZMA frame 数据；暂不支持 lazer 的额外格式语义，也不自动处理 DT/HT 的时间比例。

### 手工锚点（旧路径）

`tools/analyze.py` 保留人工锚点对齐，使用只读的 replay frame 时间坐标。建议至少三个锚点分布在整局，并保留额外事件做独立验证；单个锚点只能确定偏移，两个锚点无法提供有意义的残差检验。

```csv
host_ms,replay_ms
1000,500
11000,10500
```

```powershell
python tools/analyze.py capture.jsonl --replay play.osr --anchors anchors.csv --out output/session
```

输出 `samples.csv`（原始报告及对齐时间）、`replay.csv`（按键状态位）、`summary.json`（比例、偏移及残差）。osu!standard 的 keys 位：M1=1、M2=2、K1=4、K2=8、Smoke=16；键盘事件可能同时带对应鼠标位，不能重复计数。

## 用社区状态读取器做粗对齐

replay 只有谱面时间，采集只有主机时间，两者差一个未知偏移。纯靠按键序列搜索这个偏移在密集谱面上是够用的，但它没有先验：同一段按键序列可能在好几个偏移上都匹配得不错，而错的那些会静默地给出一个「看起来合理」的结果。社区状态读取器正好补上这个先验——它知道当前正在播放的谱面时间。

- 工具用 [tosu](https://github.com/tosuapp/tosu)（gosumemory 的维护版后继，OBS 上的 osu 悬浮层大多读它）。本地 HTTP，无需鉴权：`GET http://127.0.0.1:24050/json/v2`，谱面时间在 `beatmap.time.live`，播放状态在 `state.number`（`2` = playing），另外还有 `beatmap.checksum` 可以核对是不是同一张图。旧 gosumemory 的 v1 结构（`gameplay.time.live` / `menu.state`，路径 `/json`）也接受。
- 精度：`beatmap.time.live` 来自 tosu 的**精确循环**（`PRECISE_DATA_POLL_RATE`，默认 10 ms、最小 1 ms），不是慢速的 `POLL_RATE`（默认 150 ms，那个管其余字段），也**不是**每次请求现读内存（源码：`updatePreciseState()` 里 `playTime = memory.globalPrecise().time`，`/json/v2` 直接读它）。所以一次采样最多过一个精确周期，`host - map` 整体偏大。采集器取「每个不同 map 值的**首次**观测」的下分位作为锚点，误差因此是**精确周期**量级（约 10 ms）；报告里的 `stale_ms` 就是实测的过时量，`rate` 是主机钟与谱面钟的斜率（应当 ≈ 1）。`--state-poll-ms` 只决定我们自己多久采一次，不改变 tosu 值的新鲜度。
- 锚点只是先验：窗口内搜索与无约束搜索各跑一次，**按匹配数排序，平局才归锚点**。干净的采集在真偏移上能匹配上每一次按键，别的偏移不可能更多，所以这个裁决是安全的；锚点被否掉时会明确打印出来，提醒你读取器看到的是别的图或者时钟不对。
- 不装 tosu 也能用：不加 `--state-url` 时行为与以前完全一致，一条网络请求都不发。

```powershell
uv run python tools/game_state.py --probe                    # 确认读取器应答、看到哪张图
uv run python tools/tap_capture.py --out tap.jsonl --state-url
uv run python tools/tap_capture.py --out tap.jsonl --state-url http://127.0.0.1:24050
uv run python tools/game_state.py --out state.jsonl --duration 60   # 只记录状态流
```

开启后，同一个文件里会多一条和另外两条流同一个时钟的 `state` 流：

```text
{"type": "state", "host_ns": .., "map_time": .., "state": 2, "playing": true, "checksum": ..}
```

`tools/replay_view.py` 和 `tools/osu_align.py` 读到这条流就会自动把它当先验（`--state-window-ms`，默认 250；`--no-state-window` 关闭），并在输出里带上锚点数值。

## 采集控制台（studio）

`tools/studio.py` 把「实时深度 + 采集开关 + 回放监听 + replay 查看」合成一个常驻进程：浏览器里 Start/Stop 采集，`<osu!>\Replays` 出现新的 `.osr` 就自动对齐并给出链接。它是**唯一的设备持有者**，所以不要和 `live_depth.py` 同时开；采集只发已确认的只读 `0x14` 帧。页面的 **settings** 就是全部配置（osu! 路径、每键名字/RT、设备、tosu、皮肤、端口、窗口分钟数），改动**重启后生效**。

```powershell
uv run python tools/game_state.py --probe    # 可选：先确认 tosu 在应答（粗对齐靠它）
uv run python tools/studio.py                # 打开 http://127.0.0.1:8770/
```

工作流：点 **Start capture** 开始录（一个 session 可以打很多把，全部写进 `output/captures/tap-<时间>.jsonl`：`levels` + `keyboard` + `state`），想回顾时导出那局的 `.osr` → watcher 发现文件稳定后自动 stage（写入 `output/replays/<时间>-<名字>/payload.json` 加 `replay.osr` / `beatmap.osu` / `song.<ext>`），studio 页的列表里点开就进入共享 shell `/output/replay.html?id=<id>`。

按钮上方是采集窗口输入框（分钟，默认 **10**，`0` = 不限，上限 120）：文件只保证留住最近这么久的记录，超出的部分由写线程每 60 s 原子重写一次丢掉；所以「打一把、隔一会再导出」也有个上限。为了让刚打完那局不被窗口吃掉，`state` 流里 `playing` 由 true 变 false 时会把窗口下限钉在该局起点（`PlayRuns`），下一局开始时解除，因此文件最长 = 窗口 + 一局时长（钉住的时间本身也有上限）。窗口只影响落盘，实时曲线始终是内存里的最新值。

对齐不是猜的：tosu 的 `state` 流里每次播放谱面时间都会归零，所以「最后一段属于该图 checksum 的连续播放」就是刚导出的那一局；`studio` 用这一段的 `coarse_offset` 当先验，`replay_view` 再按匹配数裁决。同一张图在同一份 session 里导出多次会都指向最后一段（预期）。tosu 没开时会退回最后一段 playing run，并在页面里明确警告无锚点。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--settings` | `output/settings.json` | Web UI 保存的设置文件 |
| `--osu` | settings | 临时覆盖 osu! 目录（派生 Songs / Replays / Skins） |
| `--port` | settings `8770` | 页面/API 端口，只绑 `127.0.0.1` |
| `--captures` | `output/captures` | session 文件输出目录 |
| `--out` | `output/replays` | 每局数据根目录（`payload.json` + 资源）；列表清单 `replays.json` 也在这里 |
| `--skin` / `--skin-url` | settings | 临时覆盖皮肤；拷一次到 `output/skin`，所有 replay 共用 |
| `--tosu-url` | settings `http://127.0.0.1:24050` | 临时覆盖状态读取器地址 |
| `--state-poll-ms` | `50` | 我们自己多久采一次状态（不改变 tosu 值的新鲜度） |
| `--window-min` | settings `10` | 临时覆盖 Start 按钮的默认窗口（分钟，`0` = 不限，上限 120） |

页面还显示实时三条深度条、设备/tosu/监听状态，以及已对齐回放的列表（来自 `output/replays/replays.json`，跨重启保留）。HTTP 接口：`GET /api/status`、`GET /api/pages`、`GET /api/settings`、`POST /api/settings`、`POST /api/pick-folder`、`POST /api/start?window_min=<分钟>`、`POST /api/stop`、`GET /events`（SSE 实时深度）。静态文件以仓库根为文档根，所以共享 shell 能直接用 `/web/replayviewer/...` 和 `/output/skin/...`。

## 时间线视图（replay + 按键深度）

replay 视图不再每局生成一份 HTML，而是**一个共享 shell**（`output/replay.html`，由 studio 每次启动从 `tools/replay_view_template.html` 重写）+ 每局一个数据目录 `output/replays/<id>/`（`payload.json` 加它自己的 `replay.osr` / `beatmap.osu` / `song.<ext>`）。shell 从 `?id=` 得知看哪一局，`fetch` 那份 `payload.json` 再渲染。改一次模板只要重启 studio，所有 replay 立即是新视图。

`tools/replay_view.py` 的 `stage_replay()` 负责算数据、写 `payload.json`、拷资源；studio 在 `.osr` 落定、且**知道它对应哪份采集**的那一刻调用它，所以时钟对齐的 offset 直接固化进数据，查看时不再重算。`output/replays/replays.json` 是列表清单（studio 页的 aligned replays），跨重启保留；老的单页 HTML 已不再生成。

顶部 playfield 渲染交给 [replayviewer-js](https://github.com/daladal/replayviewer-js)（装在 `web/replayviewer/`，默认皮肤 `web/skins/default/` 来自该仓库），它自己 parse `.osr` / `.osu`、判分、画 hitcircle / approach circle / slider / 光标 / HUD，随播放头同步——**本仓库不自己 parse osr 来渲染**。shell 只从那一局的目录取 `replay.osr` / `beatmap.osu`。

**皮肤**在 studio 的 settings 里选（或 `--skin` 临时覆盖）：皮肤会拷到 `output/skin/` 并生成 `index.json`（`loadSkinFromDir` 的清单），页面把它叠在默认皮肤上加载——**视觉和 hitsound 一起生效**，默认皮肤只作缺项回退。osu! 皮肤常把数字放在子目录（`HitCirclePrefix: numbers/default`），所以清单保留相对路径。皮肤是全局的，所有 replay 共用一份。

每个键的 **RT 区间**来自 studio 的实时设置（`/api/settings`），所以在 UI 改完并重启后，**已 stage 的旧 replay 也立即跟着变**；`payload.json` 里存的那份只是脱离 studio（静态服务）时的快照。

```powershell
uv run python tools/studio.py     # 打开 http://127.0.0.1:8770/ 在列表里选一局
```

播放按钮 / 空格会同时播放**音乐和 hitsound**：引擎自带 `AudioSync`，歌曲由 studio 拷到那一局的目录（`song.<ext>`），hitsound 来自皮肤自带音（缺失的会合成），播放头由音频时钟驱动，拖动 / 缩放会把音频 seek 到对应时间。控制栏右侧有**播放倍速**（输入框 0.1–2.0，默认 1.0，左边是 0.25 / 0.5 / 0.75 / 1.0 快捷按钮）和 **music / hitsound 两个音量滑条**（实时生效，默认 70% / 90%）。浏览器的自动播放策略要求音频由用户手势启动，所以要先点一下 `> play`（或按空格）才有声音。

因为 playfield 用的是 ES module + `fetch` 加载皮肤，**不能直接 `file://` 打开**（浏览器会拦模块和 fetch），要从仓库根起服务；studio 自带 HTTP 服务，用 `python -m http.server` 也可以：

```powershell
uv run python tools/studio.py                    # 推荐入口 http://127.0.0.1:8770/
# 或只起静态服务：http://127.0.0.1:8800/output/replay.html?id=<id>
```

| bar | 内容 |
| --- | --- |
| 进度条 | 全曲音符密度；紫线是播放头，淡紫块是当前视窗；拖动或滚轮缩放 |
| bar 1 | 正确 note 时机 vs 实际按下时机：竖线是 note，圆点是 replay 记录的按下，两者用斜线连接，按 OD 判定着色（绿 300 / 黄 100 / 橙 50），方框是没配上任何 note 的多余按下 |
| bar 2 | Z 电平，mm（0 在顶部 = 静止，向下变深） |
| bar 3 | X 电平，mm（0 在顶部 = 静止，向下变深） |

bar 2 / bar 3 上悬停会出现准星：竖线给时间，横线给刻度读数，轴左侧的紫色标签就是横线当前的深度，单位和手柄调参一样是 mm；空心圆是采集曲线在该时刻的真实深度，和横线的自由读数区分开。纵轴的网格线也是同一把 mm 刻度，所以能不能触发、触发了多深可以直接读出来。

有真实深度（有 capture）时，bar 2 / bar 3 上还会画出设备的 **RT 区间**（紫色虚线 + 阴影，标签在绘图区右侧，如 `RT 1.00` / `RT 3.60`）。值来自标定 JSON 的 `rt_range_mm`，也可以用 `--rt-range-mm 1.0,3.6` 覆盖。

```powershell
# 只想手动 stage 一局（不经过 studio；仍需 studio 才会出现在列表里）
uv run python tools/replay_view.py "D:\osu!\Replays\play.osr" --capture tap.jsonl `
  --out "output/replays/<id>"
```

采集与 replay 的时钟差用和 `tools/osu_align.py` 相同的按键序列匹配算出来，`--tolerance-ms` 是匹配容差。一次采集可能跨好几局（菜单、重开都在里面），`replay_view.py` 会用 replay 的谱面 MD5 从 `state` 流里挑出对应的那一局，再把 level / keyboard 裁到那一段；没装读取器时退回整份采集。另外，按住键打串键时 Windows 会发**键盘自动重复**的 down，主机侧的 down 会比 replay 多——对齐器把这类多余的 down 当噪声容忍掉，不代表手柄双击。谱面按 replay 里存的 MD5 在 `--songs`（默认 `D:\osu!\Songs`）下查找：先用文件名里的 artist / title 排序缩小范围，再逐个算哈希；也可以直接 `--beatmap` 指定。

### 没打完的一把（fail → F2）

fail 后导出的 replay，帧数据只到 fail 那一刻，所以「最后一帧之后」的 note 不可能是漏击，而是**没打到**。判定用 `frames[-1][0]` 和一个判定窗的余量：`note.time > 最后一帧 + 50 窗口` 归入 `unplayed`，不算 `missed`。图上不截断时间轴（仍按整张图展开），在 fail 处画一条红色 `stopped 40.0 s` 竖线并把右侧置灰；副标题也写上 `INCOMPLETE` 和未打到的 note 数。摘要行变成：

```text
notes  : 554  press pairs: 125  stray presses: 5  unhit notes: 2
stopped: frames end at 40.0 s (28% of the map); 427 notes were never played
```

完整一把不会有 `stopped:` 那一行（`complete` 为 true，输出与以前逐字节相同）。边界情况：fail 得太早（几乎没按键）时按键序列不足以定出 offset，`replay_view.py` 会以 `Capture and replay do not share any press sequence` 退出，studio 把这句话显示在状态栏。

### 标定

深度刻度是**线性的 50 μm/raw**，以顶部为 0：raw 0 = 0 mm、raw 79 = 3.95 mm、raw 80 = 4.00 mm（用设备读数确认过）：

```text
mm(level) = 0.05 * level
```

`cmd 0x14` 那张每键 80 项的表是出厂拟合，现在只作参考记录、不参与换算（`tools/calibration.example.json` 是键名与 `step_um` 的种子）。**键名和每键 RT 区间现在是 studio 的 Web UI 设置**（`output/settings.json`），图上按每个键画各自的两条 RT 线（`replay_view.py` 的 `--rt-range-mm` 可临时广播覆盖）。刻度是常量，所以不需要为了换算去读设备：

```powershell
# 可选：只读地导出设备出厂表作参考（不参与 mm 换算）
uv run python tools/calibration.py --out tools/calibration.json
```

## 绘图与判读

```powershell
uv run python tools/plot_depth.py tap.jsonl --out depth.html --threshold 6,4,8 --gap-ms 30
```

`plot_depth.py` 为每个开关单独统计激活区间、上升时间和「松开 → 再激活」间隔，并把过短的间隔标成可疑。阈值按开关给出（`--threshold 6,4,8`，最后一个值会重复给多出来的开关）：三个键的静息位置不同（Z 0–1、X 0、C 1–4），共用一个阈值会把 C 键的静息直接算成激活——一份 600 s 采集在全局阈值 4 下给 C 键报出 271 次假激活，按各自阈值后为 0。

双击判据：一次激活结束后，在远短于人手指可能完成的间隔内又出现激活，同时主机侧出现对应的重复按下。两者都要满足才算手柄侧的连击，只有主机侧重复则更像主键盘或系统抖动。

## 协议来源与后续

- 已确认可用的读取命令：`0x14`（逐键信息块，`payload[8]` 是该键实时电平，index 0..2；采集器/实时图走这条）、`0x15`（实时电平广播，一次含三个字节，但只有被逐键读刷新过的通道是新的）、`0x10`（逐键配置，index 0..5）、`0x19`（分段读键索引，index 0..3）。
- https://github.com/Sayobot/sayo-device-web-hid 的默认分支主要是 Angular 模板，没有磁轴解析；https://github.com/Sayobot/SayoDevice_Web 是旧配置器，不能据此推定 O3C 新版协议。
- https://sayodevice.com/pkg/sayo_lib_rs.js 暴露 `AnalogKeyInfo`、`AnalogKeyInfo2`、`BroadCastData`，WASM 字符串表给出 `raw_um`、`zero_pos`、`trigger_level`、`release_level` 等字段名，但离线驱动它需要重建 flutter_rust_bridge 的 wire 编码。
- 待办：用真实对局数据验证双击判据；osu!lazer 的 replay 格式与 Songs / Replays 布局尚未处理。
- 采到的原始 NDJSON 可用 `python tools/inspect_capture.py <file>` 检查请求、报告频率及变化字节。

## License

MIT — see `LICENSE`. Third-party components (including the modified replayviewer-js bundle and the osu! skin assets under `web/`) are listed in `THIRD_PARTY.md`.
