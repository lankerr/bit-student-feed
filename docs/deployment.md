# 部署与运维

## VPS

推荐使用普通系统用户 `bitfeed`。项目位于 `/opt/bitfeed-site`，虚拟环境位于其 `.venv`；SQLite 数据在 `/var/lib/bitfeed-site`，公开导出在 `/var/lib/bitfeed-pipeline/site`。私密配置放 `/etc/bitfeed`，由 root 写入、bitfeed 只读（目录750、配置640），不使用公开 HTTP 传输 token。

安装依赖后，复制 `deploy/bitfeed-daily.service` 和 `.timer` 到 systemd，检查路径及采集接口，再执行 `systemctl enable --now bitfeed-daily.timer`。首次手动运行 `systemctl start bitfeed-daily.service`，检查退出状态及导出文章数。每日07:50北京时间运行；即使分类接口失败，也会生成带待分拣标识的公开索引，同时服务返回失败供运维发现。

现有采集服务需提供公开 `/feed.json?days=90&limit=5000`，以及只读 `/site-data.json`（返回导出目录的 data.json）。公网只读接口不得接收密钥、数据库、私聊或分类回写。设备到 VPS 的写入通过已固定主机密钥的 SSH/SFTP 完成；不能关闭 SSH 主机校验。

本机采集应在用户登录、微信同步后周期运行；未开机不伪报新鲜度。采集成功后才更新 `source_refreshed_at`，失败保留旧数据。原始微信密钥和聊天数据库不离开设备。

## GitHub Pages

1. 在仓库 Settings → Actions → Variables 建立非私密变量 `BITFEED_SOURCE_URL`，值为 VPS 的公开 `site-data.json` 地址。这个地址不含 token。
2. 在 Settings → Pages 把 Source 选为 GitHub Actions。
3. Actions → Daily public feed → Run workflow。确认成功后访问站点、`feed.xml` 和 `activities.xml`。
4. 定时默认每日北京时间08:20；GitHub 可能延迟调度。VPS 不可达或导出格式错误时构建失败，不会用空数据覆盖现有 Pages。

学校 token 无须设置到 GitHub Secrets。GitHub 只拉公开成品；分类与密钥保留在 VPS。

## 飞书

由关涵月先在飞书测试群添加自定义机器人，取得 webhook 及可选签名密钥，填入服务端 `FEISHU_TEST_WEBHOOK`、`FEISHU_TEST_SIGNING_SECRET`。测试收件人确认来源、标题、链接和排版之后，再配置正式群变量。正式群为“北京理工大学学术实践部”，时间每周四、周日12:00北京时间。

当前不启用正式发送定时器。发送器需要 HTTP200 且业务 `code=0` 才记为成功；超时记 `unknown`，等待人工检查后处理，不盲目重试；相同目标与批次不会重复发送。不要通过手机号查找或拉陌生机器人入群。

## 故障判断

- 页面生成时间新，但来源同步时间旧：设备采集停了，检查微信同步和本机任务。
- 新消息一直待分拣：检查学校密钥、完整接口格式、模型版本和当天请求预算。
- 活动数为0：可能只有通知/回顾，或原文日期不足；不自动凑推荐。
- Pages 构建失败：检查 VPS 公开导出及 Actions 日志；上一次成功部署仍保留。
- 飞书 unknown：先在测试群确认是否送达，再决定如何处理，不删除去重记录后直接重发。
