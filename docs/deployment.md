# 部署与运维

## 当前目录与职责

| 位置 | 用途 |
| --- | --- |
| `/opt/bitfeed-site` | VPS 项目与 `.venv`，运行用户 `bitfeed` |
| `/var/lib/bitfeed-live/bitfeed.sqlite` | 当前服务器采集、来源状态、模型结果、预算和发送去重记录 |
| `/var/lib/bitfeed-pipeline/site` | 仅公开导出文件 |
| `/etc/bitfeed/runtime.env` | 私密环境变量 |
| `/etc/bitfeed/school-api.json` | 已核对的学校请求合同，不在其中写真实密钥 |
| `/var/lib/bitfeed-wechat/session.json` | 用户授权扫码产生的私密公众平台会话 |

`/var/lib/bitfeed-live` 是新建的服务器数据目录，不导入旧个人缓存。不运行本机微信读取任务。官网来源、公众号来源状态、文章、模型结果及投递记录由同一套 SQLite 流水线维护；私密会话文件单独保管，不进入公开输出。

公众平台会话所在私密路径已设置受限权限。运维时用 `stat` 核对属主及权限，确保仅授权运行用户可读取；不要输出文件正文、请求 Cookie 或 token。备份 SQLite 时使用 SQLite backup 接口或停写后一致性备份，恢复时保留内容指纹、来源状态及发送去重记录。

## 日更与检查

`deploy/bitfeed-daily.service` 执行 `server-daily`，先采集公开官网及已配置公众号来源，再分拣和导出。模型失败时仍导出已有及待分拣消息，任务返回失败供运维发现。来源失败独立记录，不能用空结果覆盖旧文章。

当前 `bitfeed-daily.timer` 已 enabled，每日 07:50 北京时间运行；仓库使用 UTC 23:50 的 systemd 日历表达式。检查时无需再次启动全量采集：

```sh
systemctl list-timers bitfeed-daily.timer
systemctl status bitfeed-daily.service --no-pager
journalctl -u bitfeed-daily.service -n 30 --no-pager
```

需要手动补跑时执行 `systemctl start bitfeed-daily.service`，结束后检查退出码、来源状态、导出数量和实际模型。首次已核对官网采集为 career 2,261 条、websites 1,035 条，近 90 天导出 1,965 条；运行后数量可能变化。它们不表示所有源成功，也不表示公众号文章已采集。

每源公开审计保留 `state/pages/fetched/count/complete/truncated/last_successful_at/error/scope`。达到页数上限表示截断，观察不到下一页不必然表示全历史结束。缺少发布日期证据时显示首次收录；活动日期单独保存。

## 公众号会话与冷却

用户已确认授权 VPS 扫码，登录后可见账号数 1；“北理就业”搜索成功。首次 `appmsgpublish` 文章列表返回 `200013`、没有文章，采集已停止并写入 30 分钟持久冷却。重启进程也不应绕过冷却，不轮换账号、IP 或接口继续冲击。

扫码授权、搜索可用和文章列表成功是三个独立检查。会话失效需在服务器受控入口重新由用户扫码；不取用本机微信数据库或学校网页登录 Cookie。公开合集目前未配置，历史公众号候选名单默认不发起批量请求。详见 [订阅调查](subscription-research.md)。

## 学校模型

学校 HiAgent 已发布“学生服务”v1.1.0，VPS 使用 V2 streaming 完成过 3 条分拣并校验入库。配置参照仓库 `config/hiagent-bit.json`，运行环境通过 `BIT_AGENT_CONTRACT` 指向服务器合同文件，`BIT_AGENT_API_KEY` 使用单独的 HiAgent 凭证。MaaS 的密钥不能混用。

API key 仅发送到学校 HTTPS 地址，禁止重定向；失败日志不包含鉴权头或完整请求、响应。默认每批 20 条、每日最多 20 次请求，失败请求也消耗预算。详情及成功条件见 [学校接口](school-token.md)。

## GitHub Pages

公开仓库为 [lankerr/bit-student-feed](https://github.com/lankerr/bit-student-feed)，初稿提交 `9887bde`。Pages Source 已设为 GitHub Actions，非私密变量 `BITFEED_SOURCE_URL` 已保存，指向 VPS 的只读公开 `site-data.json`。当前网站部署尚待验收。

在 Actions → Daily public feed 手动运行；需确认构建和 Pages 部署均成功，再核对网页、`data.json`、`sources.json`、`feed.xml` 和 `activities.xml` 能访问且时间、条数、来源状态一致。工作流计划每天北京时间 08:20，可能延迟。

GitHub 只拉公开成品，不配置学校密钥、微信会话或飞书 webhook。VPS 导出不可达或格式错误应令构建失败，不用空数据覆盖已发布内容。

## 飞书

卡片及纯文本 F2 的实际测试均返回 HTTP 200 / `code=0`；这是接口接收证据。用户尚未确认在群中看见，正式发送开关和定时器保持关闭。

由关涵月核对测试消息所在群、标题、原文链接及排版，再确认正式目标“北京理工大学学术实践部”。计划北京时间周四、周日 12:00 汇总；正式启用需要正确的生产目标及 `BITFEED_PRODUCTION_SEND=true`。webhook 和可选签名密钥只放服务端。

发送记录持久化去重。超时为 `unknown`，先人工查看群消息，不能删除记录后盲目重发；HTTP 200 还需业务 `code=0` 才记接口成功。

## 故障定位

| 现象 | 检查 |
| --- | --- |
| 页面新、来源时间旧 | 查看具体源的失败、截断和上次成功时间；页面生成不代表采集成功 |
| 公众号没有文章 | 分别核对登录、搜索、列表状态；当前 `200013` 等待持久冷却，不宣称订阅成功 |
| 新消息仍待分拣 | 检查 HiAgent 合同、凭证、应用发布版本、预算和模型失败状态 |
| MaaS 在 VPS 解析失败 | 当前 MaaS 指向校内私网；使用已配置的学校公网 HiAgent 路线 |
| 活动数少或为 0 | 核对活动日期、截止证据、官方状态与正文范围，不凑数 |
| Pages 构建失败 | 检查公开 JSON 和 Actions 日志，保留上次成功部署 |
| 飞书接口成功但群里未见 | 核对实际机器人对应群及消息记录，确认可见后才开正式定时 |
