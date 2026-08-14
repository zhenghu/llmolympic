# 安全策略

## 支持范围

| 版本 | 安全修复 |
| --- | --- |
| `0.6.x` | 支持 |
| `main` | 支持（下一版本开发线） |
| `<0.6.0`、历史提交和其他功能分支 | 不支持 |

`0.6.x` 发布线和 `main` 分支会接收安全修复。发布线以最新补丁版本为准；历史提交和
未合并功能分支不单独维护。

LLM Olympics 以本地 CLI 为主，并提供仅绑定回环地址的观战页及 capability 限定的人类参与页。模型服务返回值、
模型名称、导入数据和存档内容都应视为不可信输入；不要以高权限账户运行，也不要把 CLI
或当前没有认证与 TLS 的 Web API 直接暴露为局域网、公网或多租户网络服务。

Web API 使用独立的 SQLite 只读连接，不创建或迁移主档案数据库或实时事件 sidecar。运行中
事件由比赛进程以权限 `0600` 的独立 sidecar 后台发布；sidecar 在写入前即使用与 Web DTO
相同的字段白名单，发布失败只会关闭直播，不会改变比赛、预算、ELO 或存档。Web 只公开
白名单展示字段，并拒绝跨源 WebSocket。

阶段 4.4/4.5a 的 Web 写权限严格限于独立 `*.input.db`：比赛进程为每名浏览器 Human 显式
创建独立的 `play` 席位，浏览器再用 URL fragment 交付的 256-bit capability 对对应席位的
当前 request 做一次同源 JSON POST。capability 隔离 request 的读取与提交；公开题面和动作
事件仍可能由同机观战页展示，因此当前实现不是同一操作系统账户内的保密边界。
服务端仅保存 capability 摘要；request ID、submission ID、截止时间、租约与 SQLite CAS 防止
重放、旧题串线和双标签覆盖。POST 要求精确同源 Origin、Bearer header、JSON content type，
并限制请求体和 move 大小；它不能创建比赛、选择/调用 Provider、读取凭据、写正式档案、
更新 ELO 或控制其他席位。input sidecar 权限为 `0600`，会暂存题面及尚未被引擎消费的 move，
因此不得提交、同步或当作正式档案备份；停止比赛与 Web 后可以删除。

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
