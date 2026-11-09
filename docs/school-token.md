# 接入学校 DeepSeek

已在学校现有“学生服务”发布配置中确认：API 运行中，基地址为 `https://agent.bit.edu.cn/api/proxy/api/v1`，APPID 为 `d40cplanj0bs73aatf90`。控制台存在一条掩码显示的旧密钥；完整值尚未取到。编排预览显示 DeepSeek-V4-Flash 正式版-260731；已发布旧版本仍显示 DeepSeek-R1，因此还需要核对实际调用版本后再启用。

**基地址不是可直接调用的完整接口。** 当前没有得到学校 API 的正式请求样例，配置会拒绝凭猜测发送 token。需从平台接口说明/管理员获取完整的请求 URL、鉴权头、请求体、非流式返回结构；不要套用其他厂商的 OpenAI 或 Dify 协议。

1. 把 `.env.example` 放到 VPS `/etc/bitfeed/runtime.env`；设置 `BIT_AGENT_API_KEY`，不要提交到 GitHub。运行用户仅需读取权限。
2. 把 `config/school-api.example.json` 放到 `/etc/bitfeed/school-api.json`。按学校实际说明填写 `url,headers,body,response_path`。`response_path` 以点分隔对象键或数组下标，例如 `data.answer`。若有业务状态字段，还需配置 `success_path,success_value`。
3. `body` 里的 `{{query}}` 会替换为本批次指令与文章，`{{app_id}}` 替换为 APPID；鉴权头里的 `{{api_key}}` 替换为密钥。`model` 记录实际使用的模型版本。核对后才把 `confirmed_from_platform` 设为 `true`。
4. 用少量公开文章执行 `python -m bitfeed --data /var/lib/bitfeed-site classify`。成功标准是模型返回全部请求 ID、内容指纹与结构化分类，校验后落库。仅 HTTP200 不代表分拣成功。
5. 检查结果和实际模型版本后，由每日服务自动执行。密钥只发送到 `agent.bit.edu.cn` 的 HTTPS 地址；禁止重定向，不使用网页登录 cookie。

额度限制：`BIT_AGENT_DAILY_REQUEST_LIMIT=20`、`BIT_AGENT_BATCH_SIZE=20`、`BIT_AGENT_MAX_INPUT_CHARS=20000`。这些是请求/字符预算，不是精确 token 计量；实际 token 统计以学校平台为准。密钥缺失时发布未分拣消息；配额/接口失败会记作失败并停止本轮模型请求，已有结果继续保留。

若旧密钥没有保存，可在学校平台创建仅用于本项目的新密钥。创建凭证需要账户持有人确认；不要把它贴到聊天、Issue、网页或 README 中。完成配置后可在平台撤销不用的密钥，但本项目不会自动删除旧凭证。
