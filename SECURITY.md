# 安全策略

## 支持范围

| 版本 | 安全修复 |
| --- | --- |
| `0.11.x` | 支持 |
| `main` | 支持（下一版本开发线） |
| `<0.11.0`、历史提交和其他功能分支 | 不支持 |

`0.11.x` 发布线和 `main` 分支会接收安全修复。发布线以最新补丁版本为准；历史提交和
未合并功能分支不单独维护。

LLM Olympics 以本地 CLI 为主，并提供仅绑定回环地址的观战页、capability 限定的人类参与页
及 admin capability 限定的比赛控制台。模型服务返回值、模型名称、导入数据和存档内容都应
视为不可信输入；不要以高权限账户运行，也不要把 CLI 或当前没有认证与 TLS 的 Web API 直接
暴露为局域网、公网或多租户网络服务。

观战、回放和恢复预览对主档案数据库与实时事件 sidecar 只使用独立的 SQLite `mode=ro` /
`query_only` 连接，不创建、迁移或修改它们；Web 可写状态严格限于下述 input/jobs sidecar，
FastAPI 不直接打开主档案写连接。运行中事件由比赛进程以权限 `0600` 的独立 sidecar 后台发布；
sidecar 在写入前即使用与 Web DTO 相同的字段白名单，发布失败只会关闭直播，不会改变比赛、
预算、ELO 或存档。Web 只公开白名单展示字段，并拒绝跨源 WebSocket。

阶段 4.4/4.5a 的 Web 写权限严格限于独立 `*.input.db`：比赛进程为每名浏览器 Human 显式
创建独立的 `play` 席位，浏览器再用 URL fragment 交付的 256-bit capability 对对应席位的
当前 request 做一次同源 JSON POST。capability 隔离 request 的读取与提交；公开题面和动作
事件仍可能由同机观战页展示，因此当前实现不是同一操作系统账户内的保密边界。
服务端仅保存 capability 摘要；request ID、submission ID、截止时间、租约与 SQLite CAS 防止
重放、旧题串线和双标签覆盖。POST 要求精确同源 Origin、Bearer header、JSON content type，
并限制请求体和 move 大小；它不能创建比赛、选择/调用 Provider、读取凭据、写正式档案、
更新 ELO 或控制其他席位。input sidecar 权限为 `0600`，会暂存题面及尚未被引擎消费的 move，
因此不得提交、同步或当作正式档案备份；停止比赛与 Web 后可以删除。

阶段 4.5b 的本机比赛控制面使用另一枚启动时随机生成的 256-bit admin capability。管理链接
只通过可信终端或项目专属 `0700` 状态目录中的 `0600` 临时文件交给浏览器；fragment 会立即
从地址栏清除，凭证不写 jobs sidecar、比赛档案、直播事件或日志。所有 control API 都要求
Bearer admin capability；所有写请求另要求精确同源 Origin，需要 JSON 请求体的端点还要求
严格 JSON 和体积上限，任务 prepare/start/stop 另要求幂等键。jobs sidecar 只保存脱敏的不可变
比赛配置、准备态摘要、任务状态、子进程引用和最终档案
引用，不保存 jobs 租约。权限为 `0600` 的独立 manager lock 文件提供单控制器互斥；它不代表
任务存活性，也不替代循环赛或锦标赛 checkpoint 的 runner lease。Web 服务或受控 worker
运行时不得删除 jobs sidecar 或 manager lock；两者在服务和 worker 全部停止后可删除并按需
重建，不是正式档案。

浏览器控制面只能选择内置 Human/mock 或预先配置的 Profile ID，不能提供环境变量名、
base URL、数据库路径、shell 命令或任意模型覆盖。持有 admin capability 的管理页可为已配置
`api_key_env` 和 `default_model` 的 OpenAI Profile 录入或清除 API Key；Ollama、未知或不完整
Profile 不接受。浏览器提交的 Key 绑定到 Profile ID 及由全部无凭据 Profile 字段派生的配置摘要，
不会在摘要不同的配置上复用。配置不会在 Web 服务会话中热重载；修改后需要重启服务，而重启
也会清空网页录入的 Key。若配置在 prepare 与 child 启动间漂移，child 的独立摘要复核会在
Provider 构造前安全失败。

Profile 的 `api_key_env` 只接受大写凭据型名称，并须以 `_API_KEY`、`_TOKEN`、`_SECRET` 或
`_KEY` 结尾；`LLMOLYMPIC_*` 控制协议名也被保留。这样浏览器提交值不能被误路由为
`PYTHONPATH`、动态加载器、代理、证书或其他会改变 child 执行/网络行为的环境变量。

应用不主动持久化该 Key 或其哈希：它不进入 `config.toml`、SQLite 主库或 sidecar、job spec/预览、
正式档案、URL/fragment、Web Storage、业务日志或应用级请求体日志。它仍会短暂存在于浏览器
表单/HTTP 请求体与 Python 进程内存。清除只是最佳努力地移除应用引用，不是可验证的物理内存擦除；
也不能保证浏览器扩展、DevTools、密码管理器、操作系统换页/崩溃转储或同账户其他进程不可见。

prepare 只验证请求并保存不可变准备态、工作量预览和受信 Profile 的无凭据安全投影，
不构造或调用 Provider。start 必须经过第二次确认，重新核对精确预览与完整 Profile 摘要，
再以 `shell=False` 的固定参数和最小化环境启动独立 CLI worker。控制器仅把任务所用 Profile 的 Key
按其已配置 `api_key_env` 复制到该 child，child 在构造 Provider 前独立复核摘要。继续支持从
`llmolympic web` 的启动环境变量取得 Key；网页清除不会删除该环境值。网页提交的 Key 在服务
重启后失效，checkpoint 恢复前必须重新录入或从启动环境提供。

清除 Key 只影响之后启动的任务；已运行 worker 保留自己的 child 环境副本，在途请求也无法撤回。
需要立即阻止后续使用时，应停止相关任务或在 Provider 端撤销/轮换 Key。admin capability 持有者
在 Key 就绪期间可启动并使其发生 Provider 调用；这些调用仍需完整的调用、输入 Token、单次输出、
总输出和估算费用硬上限，但本地估算不是 Provider 端的账号或账单限额。FastAPI 不直接调用
Provider，也不写正式档案、预算或 ELO。

worker 中断后的 `play`/`series` 不自动重跑。循环赛和锦标赛只允许显式恢复：resume 请求只能
携带对应 checkpoint ID，不能携带新配置或预算；未过期的对应 runner lease 仍在活动时拒绝恢复。
Web 只接受全 Mock 且无预算的 checkpoint，或者只含命名 Profile、并已有冻结硬预算的
checkpoint；恢复只使用 SQLite 中冻结的 policy、路由价格和输出上限，当前配置不能覆盖。
旧式直接 Provider checkpoint 不具备可绑定的 Profile 配置身份，只能继续从 CLI 恢复。

自 v0.11.0 起，本机控制面可准备、确认启动、停止 4/8/16 名非人类选手的锦标赛，并显式恢复
满足完整 checkpoint、runner lease、冻结 Profile 和持久预算约束的中断锦标赛。新建的 Profile
锦标赛会在首次 Provider 调用前原子创建空 checkpoint 与冻结预算；恢复预览只读核对持久状态，
浏览器不能覆盖项目、选手、seed、超时、评审团或预算。锦标赛 runner lease 以摘要、generation
和到期时间提供排他所有权；心跳维持活动 generation，过期接管会提升 generation，checkpoint、
预算与最终封存都拒绝旧 generation 写入。接管时释放旧执行者尚未调度的预留，已调度但无法确认
用量的 Provider 请求按完整上界保守记账。

stop 对准备态任务直接取消；对活动 worker，POSIX 上只终止该任务拥有的进程组，并按 SIGINT、
SIGTERM、SIGKILL 做有界升级，其他平台则对直接子进程 best-effort terminate/kill。操作幂等但
属于 best effort，不能撤回已经 dispatch 的 Provider 请求，也不保证停止后不会产生相应费用；
中断锦标赛只保留最后完整整轮 checkpoint。Live schema v2 只把
双局结果先发布为 provisional；整轮 checkpoint 事务成功后才发布 committed 确认，最终档案事务
成功后才公开冠军和档案链接。比赛进程是 Live sidecar 的唯一写者，Web 只读；直播失败不会改变
比赛、预算、ELO 或档案。浏览器仍不能直接调用 Provider，也不能直接写主档案、预算或 ELO；
这些操作继续由显式 start 后的固定参数 CLI worker 持有。

React 页面把选手名、题面、提交和失败原因作为纯文本渲染；页面 CSP 只允许同源静态资源和
精确同源 WebSocket，API 响应继续禁止加载任何内容。若未来需要远程访问，
应在项目正式提供身份认证、授权、TLS、限流和安全日志后再启用，不能用反向代理简单转发
当前本机接口来替代这些边界。

## 报告漏洞

请使用仓库的
[私有安全通告](https://github.com/zhenghu/llmolympic/security/advisories/new)
报告漏洞，不要在公开 Issue 中粘贴 API Key、访问令牌、完整模型响应或私人对局档案。

报告中请包含受影响版本、复现条件、预期影响和最小化复现步骤。若凭据可能已经泄露，
请先在对应 Provider 控制台撤销并轮换凭据。
