# 学校 DeepSeek V4 Flash 接入

当前生产路线为 VPS → 北理 HiAgent 公网 API → 已发布“学生服务”v1.1.0 → DeepSeek-V4-Flash 正式版-260731。VPS 已通过 V2 streaming 完成一批 3 条消息分拣，结果为 `state=ok, classified=3, requests=1`，文章 ID、内容指纹及结构化结果已校验入库。这证明小批调用可用，不等于全部历史文章已经分拣。

该批返回 input 4,423 / output 2,229 tokens，通过校验的可参加活动为 0。当前暂不自动扩大批次，先检查结果与真实活动样例。

此前本机 MaaS 直接调用也完成过 3 条实测，但 `maas.bit.edu.cn` 在本机解析到校内私网地址，VPS 无法正常解析/到达。MaaS 和 HiAgent 使用不同凭证；不通过改 hosts、复制浏览器 Cookie 或开放本机转发维持日更。

## 已核对协议

基地址为 `https://agent.bit.edu.cn/api/proxy/api/v1`，应用 ID 为 `d40cplanj0bs73aatf90`。请求格式根据学校控制台的 [智能体 API 文档](https://agent.bit.edu.cn/platform/doc/api/agent-api-call/agent-api-documentation) 核对；文档需要学校登录。

两次 POST 均通过 `Apikey` 请求头鉴权，并设置 `Content-Type: application/json`：

1. `/create_conversation`：传入 `UserID` 及会话名称，读取 `Conversation.AppConversationID`。
2. `/chat_query_v2`：传入 `UserID,AppConversationID,Query,ResponseMode`。当前 `ResponseMode=streaming`，读取 SSE 的最终完整 `answer`，再解析消息分类 JSON。

blocking 模式此前调用失败，当前已切换为实测成功的 streaming。不能把只收到 HTTP 200、部分 SSE 文本或创建会话成功当成分拣完成；必须收到完整结束事件和非空答案，全部请求 ID 与指纹相符、日期证据可验证后才写入分类。

## 服务器配置

用户已批准创建项目专用 HiAgent API key，并私密保存。仓库 `config/hiagent-bit.json` 是不含真实密钥的请求合同；其中 `{{api_key}}`、`{{query}}`、`{{conversation_id}}` 在运行时替换。通用 `school-api.example.json` 保持未确认模板，不是当前生产配置。

在 `/etc/bitfeed/runtime.env` 中维护：

```dotenv
BIT_AGENT_API_KEY=<仅在服务器填写>
BIT_AGENT_APP_ID=d40cplanj0bs73aatf90
BIT_AGENT_CONTRACT=/etc/bitfeed/school-api.json
BIT_AGENT_DAILY_REQUEST_LIMIT=20
BIT_AGENT_BATCH_SIZE=20
BIT_AGENT_MAX_INPUT_CHARS=20000
```

将已经核对的 `config/hiagent-bit.json` 部署到合同路径，由运行用户只读。密钥不写入 JSON、GitHub、Pages、Issue 或日志，也不设置为浏览器端变量。学校智能体负责模型调用；VPS 保留消息、定时任务、增量及校验结果，GitHub 只读取公开输出。

需要核验配置时，使用少量公开消息先跑一批并检查任务结果；日更服务已经包含分拣步骤，不要同时额外启动重复的全量分类任务。独立命令为：

```sh
python -m bitfeed --data /var/lib/bitfeed-live classify
```

该命令需运行在已有私密环境配置的服务上下文中，勿把密钥作为命令行参数。它会消耗当天请求预算，不是无副作用的健康检查。

## 缓存、预算与降级

模型主要用于新增或正文、提示词版本变化的消息。结果按文章 ID 和内容指纹缓存，活动日期与截止日期必须有源文证据；只有标题或摘要时明确保留正文范围和待核验状态。

默认每批最多 20 条、每日最多 20 次模型请求、输入字符预算 20,000；失败或结果不明的请求也计预算。这是请求/字符限制，不是准确的 token 计量；实际额度、可用模型和收费情况以学校平台为准，不承诺无限免费。

当前 HiAgent 合同只配置 V4 Flash，没有启用其他模型的自动切换。错误、超时、配额限制或不完整答案时保留待分拣并等待后续运行；已有合格结果继续公开。后续若启用备用模型，需在合同中明确配置、限制触发条件，并在每条结果显示实际模型，不把备用结果标作 V4。
