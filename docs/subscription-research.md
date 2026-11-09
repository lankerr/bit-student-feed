# 公众号订阅调查与覆盖边界

## 当前路线（2026-11-10）

**公众号后台的跨号文章列表已关闭。** 2026-07-30 起，`/cgi-bin/appmsgpublish`（以及 `appmsg`）对第三方公众号第一页就返回 `{"base_resp":{"ret":200013,"err_msg":"freq control"}}`，等待和重试都不能恢复：

- wechat-article-exporter 维护者在 [#200](https://github.com/wechat-article/wechat-article-exporter/issues/200) 宣布上游核心接口被微信关闭并停止维护，[#199](https://github.com/wechat-article/wechat-article-exporter/issues/199) 是同日的大量 200013 报告；
- [zenskill-gzh-archive 的实测记录](https://github.com/renzheng0415/zenskill-gzh-archive/blob/main/references/platform-change.md)（2026-09-29）：`searchbiz` 与单篇正文正常，`appmsgpublish` 对所有目标号第一页即 200013；
- 本项目在 VPS 上两次请求“北理就业”列表（2026-11-09 23:07、23:41），均为 200013，间隔超过 30 分钟。

公众号后台登录仍然有用：`searchbiz` 可把名称解析成 fakeid（`bitfeed/discover.py`），且访问后台首页会重新下发登录 Cookie、有效期顺延 4 天（2026-11-10 实测 `slave_sid` 等 7 项从 11-13 15:00 顺延到 11-13 17:07 UTC），`bitfeed-keepalive.timer` 据此保持登录。

**文章改由微信读书网页版获取。** 依据 we-mp-rss 固定提交 [`9fd4edb`](https://github.com/rachelos/we-mp-rss/tree/9fd4edb2a9311bffa571db050f53f415705db294) 的 `driver/weread_qr.py`、`core/wx/model/weread_mp.py` 和 `docs/weread-mp.md`，并在 VPS 上实测：

| 步骤 | 接口 | 实测 |
| --- | --- | --- |
| 扫码登录 | `GET /api/auth/getLoginUid` → 二维码内容 `https://weread.qq.com/web/confirm?uid=` → 长轮询 `GET /api/auth/getLoginInfo` | 成功，得到 `wr_vid`/`wr_skey`/`wr_rt`/`wr_ql` |
| 会话检查 | `GET /web/shelf/sync?userVid=&synckey=0` | 正常 |
| 关注 | `POST /web/shelf/add {"bookIds":["MP_WXS_…"]}` | `succ=1` |
| 完整列表 | `GET /web/mp/articles?bookId=&offset=` | 恒为 `-2041`（已下线） |
| 最新一篇 | `GET /api/mp/cover?bookId=` | 正常，返回 `reviewId` 与标题 |

书号与 fakeid 一一对应：`MP_WXS_` 加上 `__biz` 的 base64 解码数字（北理就业 `Mzg5ODcwMTAyMA==` → `MP_WXS_3898701020`）。文章永久链接由 `reviewId` 末段得到（`~` 还原为 `_`）：`https://mp.weixin.qq.com/s/<token>`。

续期按网页版客户端实现（逆向 `cdn.weread.qq.com/web/wrwebnjlogic/js/app.*.js`）：只有请求返回 `-2012`/`-2010` 时，才 `POST /web/login/renewal {"rq": encodeURIComponent(失败路径), "ql": wr_ql=="1"}`，返回 `succ=1`（带新 `wr_skey`）或 `-12013` 后重放原请求；会话仍有效时调用续期会得到 `-2013 鉴权失败`。

**覆盖边界：** cover 接口每次只给出一个号的最新一篇，因此单次推送里的第二篇及以后看不到；轮询间隔内同一号连发两次时，较早的一次也可能漏掉。官网和就业网的结构化接口不受此限，仍是活动信息的主要来源。

**名单：** `config/wechat-accounts.json` 以运营者 2026-11-03 本机导出中已关注的北理相关号为起点（只取名称与 `__biz`，不涉及聊天内容），由公众号后台搜索补充已认证的学院、书院和职能部门号（`config/wechat-discovery.json`，只采纳已认证且名称同时含学校与单位关键词的结果）。

以下为 2026-11-09 的早期调查记录。

目标是让 VPS 自行采集公开来源，订阅者只需访问一个网页或 RSS。学校智能体只做语义分拣；公众号身份、列表权限、采集分页和增量状态仍由采集器维护。公开文章不意味着所有账号的完整列表无需授权即可枚举。

## 已验证到哪一步

| 来源 | 当前证据 | 可声称的范围 |
| --- | --- | --- |
| 北理就业官网 | 公开栏目真实采集一轮 2,261 条，含招聘、实习、宣讲、双选、指导活动、通知等 | 已配置栏目及实际采集页；不能代替全部公众号文章 |
| 学院等官网 | 30 个已配置官方 URL，一轮 1,035 条，部分失败或分页未穷尽 | 成功获取的公开列表页，不是全校历史 |
| 公众平台 | 用户授权 VPS 扫码成功，账号数 1；“北理就业”搜索 `ret=0`，别名 `bitjiuye` | 登录和搜索已通；文章列表未通 |
| 北理就业文章列表 | 首次 `appmsgpublish` 返回 `ret=200013`，0 条；已停止并持久冷却 30 分钟 | 不能称为已订阅、不能用 0 条证明该号没有活动 |
| 公开合集 | 适配器存在，尚未找到并配置北理就业的真实合集 | 未配置，不计成功来源 |
| 官方历史账号名单 | 91 个候选，有 2019、2024 等出处 | 查漏候选，不是 2026 年现行完整矩阵 |

去重及近 90 天筛选后的公开导出为 1,965 条。数量和日期窗口不同，不能把两类采集数简单相加当作网站实时条数。

## 路线比较

| 路线 | 实际依赖与范围 | 本项目决定 |
| --- | --- | --- |
| 官方网站公开列表 | 公共 HTTP 列表、真实分页和发布日期证据；无需本机微信 | 作为当前稳定基础，每源独立记录失败和截断 |
| we-mp-rss 的 `web/api/app` 等模式 | 不同管理端接口及授权会话；扫码不是目标公众号列表权限保证 | 借鉴已核对协议，独立会话保留在 VPS；列表出错停止并冷却 |
| we-mp-rss 的 `free_publish` | 固定源码依次探测多个发布管理接口，遇限流可继续探测下一个 | 本项目不照搬遇 `200013` 继续切接口的行为 |
| we-mp-rss 的 `weread/weread_mp` | 微信读书会话、该平台收录及对应接口；`weread_mp` 配置需 Cookie 和 ticket | 是另一条有授权和覆盖限制的路线，尚未接入，不宣称免登录全量 |
| wechat-article-exporter 的第三方账号列表 | 原理是登录公众平台后查询其他公众号文章；维护者已于 2026-07-30 宣布上游核心接口关闭并停止维护 | 只作协议与历史实现证据，不列为已证实可用的修复路线 |
| WeWe-RSS | 源码默认依赖外部 `PLATFORM_URL` 转发服务，基于微信读书 | 只部署 RSS 前端或 VPS 反代不能消除该上游故障；不作为当前采集基础 |
| RSSHub 搜狗搜索 | 检索已被索引的文章，固定源码查询第一页 | 可辅助发现遗漏，不能证明账号历史或最新推送完整 |
| RSSHub 公众号栏目 / 公开合集 | 仅适用账号实际公开的首页栏目或指定合集 | 按栏目/合集订阅，不能将一个合集视为整个公众号 |

we-mp-rss 的模式来自其 [固定版本配置](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/config.example.yaml)；文章列表与回退逻辑见 [web.py](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/core/wx/model/web.py) 和 [free_publish.py](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/core/wx/model/free_publish.py)。这是本次调查锁定的主仓库提交，不代表其它版本接口可用。

WeWe 的外部基础服务配置及微信读书依赖见 [主仓库 README 固定提交](https://github.com/cooderl/wewe-rss/blob/e751c64294080d83deb1610d2667bed3cfa4b393/README.md)。项目已归档；这里没有把旧转发故障解释为微信所有订阅路线永久不可用。

RSSHub 的实际边界可核对同一固定提交的 [sogou.ts](https://github.com/DIYgod/RSSHub/blob/393d582acb033ccb448971d93f4f40367736909d/lib/routes/wechat/sogou.ts) 和 [mp.ts](https://github.com/DIYgod/RSSHub/blob/393d582acb033ccb448971d93f4f40367736909d/lib/routes/wechat/mp.ts)：后者明确是公众号首页栏目，适用有对应首页模板的账号。它们不是通用公众号全历史 API。

## 2026-11-10 静态源码复核：200013 与接口关闭公告

本节仅核对公开仓库、问题记录和小型静态资源；未读取实际会话文件，未请求微信公众平台后台，未更换接口或账号进行探测。锁定版本为：

| 项目 | 本次核对的提交 | 提交时间（UTC） |
| --- | --- | --- |
| wechat-article-exporter | [`590710b3d3cb049d0a9d31e264b6d6607b82236b`](https://github.com/wechat-article/wechat-article-exporter/commit/590710b3d3cb049d0a9d31e264b6d6607b82236b) | 2026-11-09 06:17:08 |
| we-mp-rss | [`9fd4edb2a9311bffa571db050f53f415705db294`](https://github.com/rachelos/we-mp-rss/commit/9fd4edb2a9311bffa571db050f53f415705db294) | 2026-10-20 16:46:26 |

### 两种证据分别说明什么

本项目的真实运行记录是：VPS 正常完成公众平台授权；`searchbiz` 搜索北理就业返回 `ret=0`，账号别名为 `bitjiuye`；首次针对该目标的 `appmsgpublish` 列表请求返回 `ret=200013`，未取得文章列表。它证明搜索可用、这一列表请求被拒绝，不能证明目标公众号没有新文章，也不能单凭该错误码断定原因是短期请求过快或接口永久关闭。

exporter 维护者 jooooock 在 **2026-07-30** 发布的[停止维护说明 #200](https://github.com/wechat-article/wechat-article-exporter/issues/200)写道：

> 项目所依赖的上游核心接口已被微信关闭

维护者同时说明，一键同步已无法继续；残留的 Credential 通道需要较多手工操作，未真正接入主流程，作者不把它视为替代方案。当前[固定提交 README](https://github.com/wechat-article/wechat-article-exporter/blob/590710b3d3cb049d0a9d31e264b6d6607b82236b/README.md)保留停维告示，并记载在线网站于 **2026 年 10 月**下线。此前的[问题 #199](https://github.com/wechat-article/wechat-article-exporter/issues/199)记录了用户从正常同步转为 `200013:freq control` 的现象。

这些是一手项目状态和运行报告，不是微信官方发布的接口关闭公告；也不证明所有账号、所有公众号入口均永久不可用。结合本项目现场结果，可以确定当前第三方公众号列表尚未接通，不能据历史源码或成功扫码宣布订阅成功。

**30 分钟只是本项目本地设置的保守停请求时间，不是微信返回的恢复时间或服务承诺。** 冷却结束不等于权限恢复，不触发自动切换端点、账号或再次批量探测。本轮保持停止真实第三方列表请求。

### exporter 的真实第三方列表请求与当前实现的差异

固定提交的 [`server/api/web/mp/appmsgpublish.get.ts`](https://github.com/wechat-article/wechat-article-exporter/blob/590710b3d3cb049d0a9d31e264b6d6607b82236b/server/api/web/mp/appmsgpublish.get.ts#L29)组装 `GET /cgi-bin/appmsgpublish`，并非公开合集接口。无关键词列表分支使用 `sub=list`、`sub_action=list_ex`；目标账号由调用方 `id` 传给 `fakeid`，分页由 `begin/count` 控制。公共参数为登录存储中的 `token` 以及 `lang=zh_CN`、`f=json`、`ajax=1`。

| 字段 | exporter 固定源码 | 当前项目 / WeRSS 列表模板 |
| --- | --- | --- |
| `search_field` | 无关键词为字符串 `'null'`，搜索为 `'7'` | 未设置 |
| `query` | 原样取调用方 `keyword`；不是源码固定常量 | 未设置 |
| `type` | `'101_1'` | `appmsgpublish` 未设置；另一个 `appmsg` 模板的 `type=9` 不能混为同一参数 |
| `free_publish_type` | `1` | 未设置 |
| `fingerprint` | 此列表参数中未出现 | 当前列表参数中未出现 |

请求头见固定提交 [`proxy-request.ts`](https://github.com/wechat-article/wechat-article-exporter/blob/590710b3d3cb049d0a9d31e264b6d6607b82236b/server/utils/proxy-request.ts#L17)：它设置 `Referer=https://mp.weixin.qq.com/`、`Origin=https://mp.weixin.qq.com`、固定 `User-Agent` 和 `Accept-Encoding=identity`，并从存储中加入授权 Cookie。其 [`config/index.ts`](https://github.com/wechat-article/wechat-article-exporter/blob/590710b3d3cb049d0a9d31e264b6d6607b82236b/config/index.ts)使用 Chrome 117 字符串加 `WAE/1.0`。代理允许跟随重定向。这里的 `X-Auth-Key` 是应用查找自身 Cookie 存储的键，不是传给微信列表接口的额外凭证；代理没有添加 `fingerprint` 请求头。

当前项目使用固定 `bit-student-feed/0.2` User-Agent、正确的根路径 Referer，未设置 Origin；保持 HTTPS 证书校验、禁止重定向、明确超时。WeRSS 的 [`base.py`](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/core/wx/base.py#L175)则随机选择 User-Agent，并把来源头拼作 `Refer`，不是标准 `Referer`。其 `web.py` 与 `free_publish.py` 请求包含 `verify=False`；这些行为不作为本项目应复制的要求。

以上是可复核的协议差异，**没有证据证明增加这些参数、复制 User-Agent 或补 fingerprint 可以恢复当前的第三方列表权限**。exporter 停维说明尤其不能被反向解释成某套隐藏参数仍可用；本节没有据此修改请求或发送新的真实请求。

### free_publish 能否证明返回的是第三方账号

WeRSS 的 [`free_publish.py`](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/core/wx/model/free_publish.py#L33)依次配置 `free_publish?action=list`、`publish?action=publish_list`、`appmsgpublish`、`appmsg`。源码把 `fakeid` 填进查询串，但这本身不能证明服务端接受该参数并按它查询第三方账号。

它的有效性判断只检查非空列表或非空 `publish_page.publish_list`；随后将请求目标的 `Mps_id` 写入文章，不核对返回文章永久链接中的 `__biz` 是否等于目标 `fakeid`。因此，即使某发布管理接口返回 `ret=0` 和文章，也不能仅凭该状态认定是北理就业。静态源码尚不足以断言这些接口在所有情况下“只返回自己号”，同样不足以证明它们支持第三方全量列表。本项目保留逐篇目标 `__biz` 一致性校验，归属不符或无法验证时停止，不导入目标账号。

WeRSS 的[问题 #469](https://github.com/rachelos/we-mp-rss/issues/469)和[关闭的 PR #470](https://github.com/rachelos/we-mp-rss/pull/470)讨论了 `200013` 后提前退出、回调重复等问题。PR 描述中的实际成功案例是降级到 **`weread_mp`**，不是证实第三方 `free_publish` 列表恢复。该 PR 未合入本次锁定主线；主线 `web.py` 仍在有限重试后停止，`free_publish.py` 仍会依次试探多个后台端点。本项目不照搬该试探流程，也不把 `weread_mp` 的收录范围和授权会话说成公众平台原接口已经修好。

失败时还必须保留增量边界。WeRSS 的[问题 #440](https://github.com/rachelos/we-mp-rss/issues/440)指出采集开始即推进 `sync_time` 会在限流后造成永久漏采；固定主线 [`WxGather.Start()`](https://github.com/rachelos/we-mp-rss/blob/9fd4edb2a9311bffa571db050f53f415705db294/core/wx/base.py#L324)仍可见此行为。列表失败、分页未完成、空响应异常均不能被本项目当作成功日更。

## 名单与查漏

候选来源包括学校 [2019 年公开目录页面](https://bit.edu.cn/tzgg17/ggxx/a172995.htm) 及其 [备案表附件](https://bit.edu.cn/docs/2019-07/20190711040006229480.xlsx)、[2024 年资产系统公开文件](https://asset.bit.edu.cn/docs/2024-06/8d790ec00290462799e12859039241b1.pdf)、[校级官方新媒体介绍](https://www.bit.edu.cn/yxw/gfxmt/a156772.htm)。历史名单用于发现遗漏，必须逐号核对当前名称、账号身份、最近文章与可用列表；默认不对 91 个候选批量发起请求。

官网校验以 [北理就业](https://job.bit.edu.cn/frontpage/bit/html/index.html) 和 `config/official-websites.json` 中的 30 个官方 URL 为起点。需额外对比“公众号有、官网无”的活动样例，才能衡量真实缺漏，不能仅凭官网条数推断公众号齐全。

## 新增来源的验收方式

1. 保存可核对的官方身份或公开栏目/合集链接；历史候选先保留“待核验”。
2. 分别记录登录、账号搜索、文章列表和实际文章样例。会话凭证仅私密保存在服务端。
3. 逐页记录页数、条数、停止原因、上次成功时间和覆盖范围。到达预算上限标截断，异常保留旧结果。
4. 区分发布日期、首次收录时间、活动日期及正文范围；链接与日期证据校验通过后进入同一 SQLite。
5. 日更后从公开 `sources.json` / 页面核对状态，再检查 RSS、每日指引与飞书的增量去重。

当前应优先修复失败官网来源，并以公开栏目、合集及官方账号身份材料补充证据。公众平台列表保持“未接通”，本轮不再请求真实第三方列表；冷却时间不能充当恢复证据。只有之后实际取得目标文章、分页和稳定增量证据，才把对应公众号从“验证中”改成“已接通”。
