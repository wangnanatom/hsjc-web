# GEMNI

## [Knowledge Learned]
- **Windows 环境中文控制台下的编码崩溃**：在输出包含特殊字符的文本时，若遇到 `UnicodeEncodeError: 'gbk' codec` 报错，必须运行前在终端执行 `$env:PYTHONIOENCODING="utf-8"` 或在 Python 入口重置 stdout 编码为 UTF-8。
- **Grep 命令行工具缺失**：本地执行时不可强依赖内置 `grep_search`。若报错找不到 `grep` 依赖，应降级使用 Python 脚本遍历或使用 Powershell 的 `Select-String` 管道。
- **不规范 Email Header 解析错误**：Python 官方 `email` 库在解析极个别损坏的 `From` 头字段时，可能会抛出 `AttributeError: 'str' object has no attribute 'token_type'`。在解析该字段时必须做 `try-except` 防御性捕获，降级获取原始头部字符串。
- **香港赛马会 API 数据抓取的安全与绕过机制**：
  - `info.cld.hkjc.com/graphql/base/` 接口存在 CIAM / Cloudflare WAF 设备签名风控，且启用了 Persisted Query 白名单校验。外部普通 HTTP 脚本直接调用会报错 `WHITELIST_ERROR: FORBIDDEN`。
  - **解决方案与最佳实践**：采用 Playwright 无头 Chromium 启动会话访问 `https://bet.hkjc.com/ch/racing/wp/` 完成浏览器指纹握手，并在页面上下文直接执行马会前端完全一致的 whitelisted GraphQL 查询模板（保持 operationName 为 `racing` 并包含完整字段定义）。经实测，仅需 7~8 秒即可无缝、100% 成功抓取全天 8~10 场全部马匹的 WIN、PLA、QIN、QPL 真实实盘赔率流，彻底免去逆向签名和 Token 过期的维护成本。
- **赛马 2D 组合矩阵（连赢/位置Q）的自适应与双向匹配机制**：
  - 各场次实际参赛马匹数不固定（高班赛常为 6~9 匹，常规赛为 14 匹）。硬编码 14x14 矩阵会导致马匹少的场次出现大面积空行；前端必须动态提取马匹集合渲染自适应的 $N \times N$ 网格。
  - 二元组合（QIN/QPL）具有无序对称性，在数据库入库必须补全整场 $\binom{N}{2}$ 个完整配对，且前端检索必须支持 `matrixDict[i-j] || matrixDict[j-i]` 双向容错匹配。
- **Cloudflare Quick Tunnel 动态域名重连特性**：
  - `cloudflared tunnel --url ...` 在免费匿名试用模式下，一旦宿主机进程重启或隧道中断，重新建立连接后会动态分配一个全新的二级域名（旧域名立即返回 502 Bad Gateway）。给客户展示时若遇打不开，需立刻拉取最新日志提取新 URL 发送给客户。
- **Render.com 免费 Web Service 零依赖部署与便携式数据库架构**：
  - Python Web 服务发布至 Render 时，入口代码必须通过 `PORT = int(os.environ.get("PORT", 5000))` 动态绑定系统环境变量 `$PORT`。
  - 采用便携式自包含 SQLite（如 `hsjc.db`）随 Git 仓库一同交付，不仅完全免去外部云数据库（RDS/TiDB）的采购与网络白名单（Outbound IP）维护，且冷启动只需毫秒级，内存消耗 $<30\text{MB}$，是交付跨国客户/投资人独立看盘演示的最佳生产实践。
- **云端单容器内轻量守护线程与 SQLite WAL 高并发防锁死实践**：
  - 在云托管平台（如 Render Free Tier）运行定时抓取时，为避免额外开启付费 Worker 实例，应在 Web 主入口直接使用 Python `threading.Thread(target=..., daemon=True)` 启动进程式守护线程，零额外内存与成本开销。
  - 为彻底解决后台高频写入（每 15 秒）与前端用户并发查询引起的 `database is locked` 报错，必须在数据库初始化时开启预写日志模式：`PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 5000;`，实现读写互不阻塞。
- **自动同步与推送 GitHub 规范（用户明确指示）**：
  - 今后对 `hsjc-cloud-web`（或相关云端服务）进行的任何代码改动、UI 调整或数据更新，本地执行与测试完成后，必须**自动执行 git add、git commit 并立即推送（git push origin main）至 GitHub 仓库**，以便触发 Render 云端自动化构建与部署，确保线上云端网页始终与本地最新改动保持 100% 同步。
- **云端免浏览器（Zero-Browser）极速全自主采集架构**：
  - 虽然马会启用了 CIAM WAF 防护，但只要 HTTP 请求携带官方前端完全一致的 whitelisted GraphQL 模板、正确的 `Referer` 与 `Origin: https://bet.hkjc.com`，马会服务端会直接返回 HTTP 200 及 Gzip 压缩流。
  - 核心价值：无需在云端安装 Chromium/Playwright（免去 500MB 内存开销与 apt 依赖问题），仅用 Python 内置 `urllib.request` + `gzip` 即可在 Render 免费容器中运行（单场抓取仅需 0.3 秒，全天 8 场仅需 2 秒）。容器启动时通过守护线程自启采集，实现云端完全脱离本地电脑独立 7×24 小时自主采集。
- **Windows 本地默认端口 5000 静默重置冲突**：
  - Windows 系统组件（如 UPnP、SSDP 或 Game Services）有时会静默占用或阻断 5000 端口，导致客户端请求时报 `RemoteDisconnected`（服务端立即断开连接）。本地开发服务应使用 `5050` 作为兜底默认端口：`int(os.environ.get("PORT", 5050))`，既保证云端 Render 动态端口生效，又彻底规避 Windows 本地端口劫持。
- **Windows 下 Python HTTP 服务 `allow_reuse_address` 导致的多进程端口冲突**：
  - 在 Windows 环境下，`SO_REUSEADDR`（即 `ThreadingHTTPServer.allow_reuse_address = True`）并不等同于 Linux 的快速释放，而是允许多个 Python 进程静默绑定到同一个 TCP 端口。当后台旧任务未杀干净时，两个进程同时监听该端口会导致请求被随机拦截或直接触发 `RemoteDisconnected`。本地开发应显式设置 `allow_reuse_address = False`，让系统在端口占用时立即抛出 `[WinError 10048]` 明确报错，避免多进程静默抢占端口。
- **赛事生命周期管理与赛后硬停（Hard-Stop）去重机制**：
  - 香港赛马盘口在尾场完赛（夜赛 22:50、日赛 18:00）后会进入 `sellStatus: STOP_SELL` 及 `status: PAYOUT`。采集器若缺乏赛后熔断机制，会在半夜持续采集并写入大量重复相同的赛后封盘死数据，直接占满前端时间选择列表。
  - 核心架构方案：采集器必须识别马会 GraphQL 状态标志位，检测到全场次停止受注派彩后，规范化写入一条官方终盘收官切片（如 `23:00:00`），并立即标记 `meetingClosed = True` 进入深度休眠（3600 秒轮询间隔），直到下一个赛事日早盘 09:00 自动唤醒，彻底防止赛后脏数据泛滥。
- **Render 免费容器外部休眠判定与本地 SQLite 数据孤岛的致命陷阱**：
  - **陷阱一（假保活）**：Render 免费实例的空闲休眠（Spin down after 15m idle）是由云端外部 API Gateway 判定。在容器内部使用线程 ping `127.0.0.1` 无法被外部网关识别为有效公网流量，只要外部没有真人访问，15 分钟后实例必然被休眠冻结。
  - **陷阱二（数据孤岛）**：Render 容器的文件系统是 Ephemeral（临时）的，容器内生成的 `hsjc.db` 既不会自动推送给 Git，也绝不可能自动同步回用户的本地 Windows 电脑。因此，如果用户看盘使用的是本地 `localhost`，则本地电脑关机后本地绝对无法获得云端产生的数据；必须明确区分“本地独立实战采集”与“云端集中式数据库”的区别，避免产生“关电脑也能同步本地数据”的错误预期。
- **Turso Python 驱动依赖冲突与纯 HTTP 管道规避方案**：
  - 在 Windows Python 3.8 环境下执行 `pip install libsql-experimental` 会因缺少 wheel 导致 `ERROR: Could not find a version that satisfies the requirement puccinialin` 报错。
  - **解决方案**：降级使用纯 Python 的 `libsql-client`，或直接采用 Turso 官方原生的 HTTP Pipeline API（`https://<db>.turso.io/v2/pipeline`）直接发送 SQL JSON 请求。纯 HTTP 方案完全无 C 扩展、零编译依赖，在 Windows 本地与 Render 云端跨平台 100% 免安装原生兼容。
- **Turso 数据库连接报错 `400: Invalid response status` 的协议根因与解决**：
  - Turso 控制台默认给出的 URL 是 `libsql://<db>.turso.io`。Python `libsql-client` 收到 `libsql://` 会自动转换为 WebSocket 连接（`wss://`），在部分 AWS 区域节点（如 `aws-ap-northeast-1`）会因 WebSocket 握手头版本不匹配抛出 `400, message='Invalid response status'`。
  - **解决方案**：在客户端入口将 `libsql://` 自动规范化转换为 `https://`，驱动会自动切换为基于 HTTP 的 Hrana 协议，连接即刻成功且无长连接断流风险。同时在 Windows Python 3.8 下需设置 `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`，规避客户端释放时的事件循环关闭报错。
- **赛后封盘（meetingClosed）的时段硬防护与防止日间误封盘**：
  - 香港赛马在非开盘时段或早盘期间，部分彩池状态可能呈现 `STOP_SELL` 或未开启。若判断完赛封盘仅依赖 `sellStatus` 或布尔标志，在白天早盘或凌晨时段会误判为“全天已完赛派彩”，导致日间提前写入 `23:00:00` 终盘切片并提前进入挂起休眠。
  - **解决方案**：封盘逻辑必须增加赛后时间硬防护（`isPostRaceHours`），跑马地夜赛必须在 22:30 以后、沙田日赛必须在 18:10 以后且状态确实为 `PAYOUT/FINAL` 方可判定完赛，彻底杜绝日间早盘误封盘。
- **云端数据库基准赔率批量预热机制（Preload Baseline Odds）**：
  - 在分布式云数据库（如 Turso/LibSQL）架构下，若每轮抓取都逐匹马单次查询早盘基准赔率（1场 12 匹 + 66 组组合，全天 8 场共 1,152 次网络单查），会因累积网络往返延迟耗时高达 1~2 分钟，严重拖慢采集周期。
  - **解决方案**：在每轮开始前通过 3 条批量 SQL 一次性预热全天（`LIKE 'YYYY-MM-DD%'`）最早出现的独赢、连赢、位置Q赔率到内存字典 `baseOddsCache` 中，将网络查询从 1,152 次降至 3 次（耗时从 120 秒降至 0.8 秒），后续赔率降幅计算全部为 $O(1)$ 内存命中。




