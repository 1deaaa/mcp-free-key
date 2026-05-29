# MCP 聚合网关 (MCP Aggregation Gateway)

一个高性能、高可用的透明反向代理网关，专门用于聚合多个支持远程 URL 接入的 MCP (Model Context Protocol) 服务。支持多账户多密钥轮询负载均衡、有状态会话粘连、多层失败检测以及连续失败永久禁用等企业级特性。

项目内置了一个美观、适配高分屏的图形配置编辑器，方便快速管理服务与密钥。

---

## 📖 密钥轮询注入方式解释

在 MCP 服务的 `key_auth` 配置中，支持两种密钥注入方式：`header` 和 `query`。它们决定了网关在向游上游转发请求时，如何将选中的密钥传递给上游服务器：

### 1. `header` 注入方式
* **含义**：网关会将选中的密钥放入 HTTP 请求的 **请求头 (Headers)** 中。
* **示例**：如果配置 `type: header` 且 `param: CONTEXT7_API_KEY`，网关在转发请求时，会在 HTTP 请求头中加入：
  ```http
  CONTEXT7_API_KEY: ctx7sk-xxx
  ```
* **适用场景**：适用于将密钥作为敏感凭证放在请求头中传输的服务（如 Context7）。

### 2. `query` 注入方式
* **含义**：网关会将选中的密钥作为 **查询参数 (Query Parameters)** 拼接到上游 URL 的末尾。
* **示例**：如果配置 `type: query` 且 `param: tavilyApiKey`，上游 URL 为 `https://mcp.tavily.com/mcp/`，网关在转发请求时，会将请求发送到：
  ```http
  https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-dev-xxx
  ```
* **适用场景**：适用于通过 URL 参数进行身份验证的服务（如 Tavily）。

---

## 🛠️ 核心特性

1. **透明反向代理**：对 MCP 协议零侵入，完美兼容 Streamable HTTP 传输协议，支持 SSE (Server-Sent Events) 流式透传。
2. **多密钥轮询负载均衡**：支持为单个服务配置多个密钥，采用 Round-Robin 算法进行请求级或会话级轮询，实现多账户负载均衡。
3. **有状态会话粘连**：针对有状态上游（如 Context7），网关会自动捕获并绑定 `Mcp-Session-Id`，确保同一会话的后续请求始终复用同一把密钥，避免因账户不一致被上游拒绝。
4. **多层失败检测与故障转移**：
   - **HTTP 状态码**：识别 `401`、`403`、`429` 等鉴权与限流状态码。
   - **JSON-RPC 错误**：解析并识别 JSON-RPC 协议层错误。
   - **正文特征匹配**：针对 Tavily 这类即使额度耗尽也返回 `HTTP 200` 的服务，支持配置专属失败特征词（如 `exceeds your plan`）进行精准识别。
   - **故障转移**：一旦判定密钥失效，自动在当前请求中无缝切换到下一把可用密钥重试。
5. **连续失败永久禁用**：如果某把密钥**连续失败达到 2 次**，系统会将其判定为“永久失效”并彻底隔离，不再参与轮询，直到用户在 GUI 中手动恢复。
6. **高分屏图形配置器**：内置 Tkinter 图形界面，支持高 DPI 完美缩放，所有字体 ≥ 13pt，支持批量导入去重、密钥状态着色（正常-绿色、冷却-橙色、禁用-红色）以及一键手动恢复。

---

## ⚙️ 参数配置说明 (`config.yaml`)

```yaml
gateway:
  port: 8080                      # 网关监听端口（默认 8080，固定监听 0.0.0.0）
  access_keys:                    # 访问网关的统一密钥列表（客户端请求时需携带）
    - "change-me-please-set-a-strong-key"
  key_cooldown_seconds: 1800      # 单次失败后的临时冷却时间（秒，默认 30 分钟）
  session_ttl_seconds: 1800       # 有状态会话的空闲淘汰时间（秒）
  max_failover_retries: 3         # 单次请求允许的最大故障转移重试次数
  upstream_timeout_seconds: 120   # 转发上游的超时时间（秒）

services:
  - name: context7                # 服务名（对应网关路由路径，如 /context7）
    enabled: true                 # 是否启用该服务
    upstream_url: "https://mcp.context7.com/mcp" # 上游 remote URL
    key_auth:
      enabled: true               # 是否启用密钥轮询
      type: header                # 密钥注入方式：header 或 query
      param: "CONTEXT7_API_KEY"   # 注入的字段名
    keys:                         # 密钥池
      - "ctx7sk-xxx"
    failure_patterns:             # 专属失败特征词（大小写不敏感）
      - "rate limit"
      - "quota"
```

---

## 🚀 快速开始

### 1. 安装依赖
确保您的 Python 环境为 3.12+，然后安装依赖：
```bash
pip install -r requirements.txt
```

### 2. 启动网关
网关默认监听 `0.0.0.0:8080`：
```bash
python start.py
```

### 3. 打开图形配置器
图形配置器支持高分屏，字体清晰美观：
```bash
python gui.pyw
```

---

## 🧪 运行测试
项目配备了完整的单元测试与 Mock 联调测试，可一键运行：
```bash
pytest
```
