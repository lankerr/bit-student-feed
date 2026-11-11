# 部署与运维

## 目录与职责

| 位置 | 用途 |
| --- | --- |
| `/opt/bitfeed-site` | 代码与 `.venv`；服务以 `bitfeed` 用户运行，工作目录在此 |
| `/var/lib/bitfeed-live/bitfeed.sqlite` | 文章、分拣结果、来源状态、模型预算、飞书发送去重 |
| `/var/lib/bitfeed-pipeline/site` | 公开导出（GitHub Pages 构建的数据来源） |
| `/var/lib/bitfeed-weread/` | 微信读书会话 `session.json`、调度状态 `scheduler.json`、运行时添加的号 `extra-accounts.json` |
| `/var/lib/bitfeed-wechat/` | 公众号后台会话 `session.json`、续期结果 `keepalive.json`、搜号结果 `resolved.json` |
| `/etc/bitfeed/runtime.env` | 私密环境变量（学校 API key、飞书 webhook、登录维护口令） |
| `/var/backups/bitfeed/` | 每次部署前的代码、配置与数据库备份 |

会话文件均为 0600。运维时不要输出文件正文、Cookie 或 token。

## 系统服务

```sh
systemctl list-timers --all | grep bitfeed
systemctl status bitfeed-web.service --no-pager
journalctl -u bitfeed-wechat.service -n 20 --no-pager
```

| 单元 | 作用 |
| --- | --- |
| `bitfeed-web.service` | 0.0.0.0:18780：`/health`、`/site-data.json`、`/feed.json`、`/weekly/<周报>.docx`、`/test-receipts`、`/ops/<口令>/` |
| `bitfeed-wechat.timer` | 每 5 分钟一次微信读书订阅（`python -m bitfeed wechat-sync`） |
| `bitfeed-keepalive.timer` | 每 30 分钟：微信读书 `shelf/sync` 续期 + 公众号后台首页续期，然后检查提醒 |
| `bitfeed-refresh.timer` | 每 2 小时及周日 11:40：分拣与导出（`refresh`，每次最多 3 批） |
| `bitfeed-daily.timer` | 每天 07:50：就业网与官网采集、分拣、导出（`server-daily`） |
| `bitfeed-digest.timer` | 每周日 12:00：一周汇总与 Word 完整版（`feishu-digest`） |

旧的 `bitfeed-pipeline.service`（读本机微信导出的测试接口）已停用，`/feed.json` 改由新服务从线上数据库提供，格式不变。

## 部署新版本

在本地仓库：

```sh
git archive --format=tar.gz -o release.tgz HEAD bitfeed config deploy docs tests pyproject.toml README.md collect_site_feed.py
scp release.tgz ty-vscode:/tmp/bitfeed-stage/
```

在 VPS（root）：先备份 `/opt/bitfeed-site`、`/etc/bitfeed/runtime.env`，用 SQLite backup 接口备份数据库，然后

```sh
cd /opt/bitfeed-site && tar -xzf /tmp/bitfeed-stage/release.tgz
.venv/bin/pip install --no-deps .
install -m 0644 deploy/bitfeed-*.service deploy/bitfeed-*.timer /etc/systemd/system/
systemctl daemon-reload && systemctl restart bitfeed-web.service
```

这台 VPS 同时承担 A800 的反向隧道跳板，部署只动 `bitfeed-*` 单元和上面列出的目录。

## 登录维护

两个登录平时自动续期。失效或即将过期时，飞书运维群会收到提醒和“登录维护”链接（即 `runtime.env` 里的 `BITFEED_OPS_URL`）。打开后：

- **扫码登录微信读书**：用任何微信扫码确认即可；新账号的书架为空时，订阅程序会自动重新关注名单里的号。
- **扫码登录公众号后台**：用「格拉摩根的指挥官」的管理员微信。桌面上的「北理消息站-登录维护」快捷方式和订阅助手的「扫码登录」都打开同一页。
- **添加订阅的公众号**：粘贴该号任意一篇文章链接，服务器从公开文章页读出账号身份，几分钟内开始订阅。

不方便开网页时，也可以在服务器上运行：

```sh
runuser -u bitfeed -- /opt/bitfeed-site/.venv/bin/python -m bitfeed weread-login   # 二维码在 /var/lib/bitfeed-weread/weread-qr.png
runuser -u bitfeed -- /opt/bitfeed-site/.venv/bin/python -m bitfeed.wechat_auth --directory /var/lib/bitfeed-wechat
```

## 订阅节奏与限流

每次运行最多 3 个微信读书请求，相邻请求至少间隔 50～80 秒（跨运行也生效），每天上限 600 个。档位：0 档每小时、1 档每 3 小时、2 档每 8 小时；连续 14 天没有新文章的号频率减半。限流（`-2014`、`-2041`、HTTP 499，或续期后仍被拒）后全部暂停，按 0.5、1、2、4、8、12 小时逐级退避，24 小时无限流后退一级。

文章正文从公开文章页读取，每次 1～2 篇，间隔 25～45 秒，每天上限 240 篇；遇到“环境异常”验证页暂停 2 小时。

## 飞书

`FEISHU_TEST_WEBHOOK` 是运营者所在的群，目前汇总（`BITFEED_DIGEST_MODE=test`）和运维提醒都发到这里。正式发到“北京理工大学学术实践部”群时，在 `runtime.env` 中设置：

```sh
FEISHU_PRODUCTION_WEBHOOK=<该群自定义机器人地址>
BITFEED_PRODUCTION_SEND=true
BITFEED_DIGEST_MODE=production
```

运维提醒想单独发到另一个群时，设置 `FEISHU_OPS_WEBHOOK`。发送结果以飞书返回的 `code=0` 为准；超时记为 `unknown`，不会自动重发。同一批文章不会重复发送，汇总窗口只在送达后前移。

### 一周汇总

每周日 12:00 发一次。飞书自定义机器人不能发送文件，所以完整内容写成 Word，放在 `http://<VPS>:18780/weekly/bit-weekly-<日期>.docx`（`BITFEED_FILES_BASE` 可改前缀），卡片里给下载链接，卡片正文只放本周要点和重点条目。

两类来源的“本周”算法不同：

- 公众号、学校与学院官网：按原文发布日落在上次汇总之后；页面没有日期的按首次收录日。Word 里按日期分组，一天一节。
- 北理就业：这是常驻列表，抓到的时间不代表新。宣讲会、双选会、指导活动只看未来 8 天内的活动日期；招聘和实习岗位以「招聘有效开始」为发布日，本周新上的单列，一周内截止的再提醒一次；通知公告按发布日期。已经结束、取消或暂缓的不列入。

学校模型每次最多看 120 条，逐条判断本周是否有用、是否优先，并写一句理由，再根据重点条目写本周要点。它有单独的每日额度（8 次，`weekly_calls`），不和日常分拣抢。模型判断没用的条目不删，放在 Word 末尾附录；模型不可用时全部保留，并在卡片和 Word 里写明。

日期一律写全年月日。

## 故障定位

| 现象 | 检查 |
| --- | --- |
| 公众号长时间没有新文章 | 登录维护页的会话状态；`scheduler.json` 中的 `cooldown_until`、各号 `state`/`error` |
| 微信读书显示已失效 | 打开登录维护页（会自动出码）用微信扫码；平时由 `bitfeed-keepalive` 续期，不必反复扫 |
| 公众号后台显示即将过期 | `keepalive.json` 的 `expires_at` 应在每次续期后顺延 4 天；不顺延时用「格拉摩根的指挥官」管理员微信在维护页扫码 |
| 新消息仍待分拣 | `refresh` 的输出、学校 API 预算（每日 20 次）、`model_retry_after`。额度用完时任务状态是 `budget`，采集和导出仍完成，次日继续 |
| 网页没更新 | GitHub Actions 的 Daily public feed 运行记录；`curl http://127.0.0.1:18780/site-data.json` |
| 飞书没收到汇总 | `journalctl -u bitfeed-digest`；`deliveries` 表中的状态 |
