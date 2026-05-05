# LiteLLM 本地代理：让 Codex Desktop GUI 对接 DeepSeek + 火山方舟

> 目标：用 LiteLLM 在本机 `127.0.0.1:4000` 起一个协议翻译网关，把 Codex Desktop 发出的 **Responses API** 转为后端可接受格式，并统一路由到 DeepSeek 或火山方舟 Coding（Kimi/GLM/MiniMax/Doubao）。
>
> 环境：macOS（darwin 25.x），Apple Silicon，已装 Homebrew 与 `uv`。

---

## 0.1 2026-05-04 现状修订（必读）

这份文档前半部分来自早期“仅 DeepSeek”版本；你当前环境已经扩展为“DeepSeek + 火山方舟 Coding”双线路。为避免混淆，以下 6 点以当前落地文件为准：

1. `~/litellm/config.yaml`：已包含 `kimi-k2.6`、`glm-5.1`、`minimax-m2.7`、`doubao-seed-code`、`ark-auto`，并给 `gpt-5.*` 做了别名映射到 Kimi。
2. 火山条目必须走 `https://ark.cn-beijing.volces.com/api/coding/v3`，且 `use_chat_completions_api: true`。
3. `~/litellm/tool_filter.py` 已包含：非 function tools 过滤、Volcano 参数剥离、schema 放松、多模态折叠、tool 链修复、火山 tools 限额与描述截断。
4. `~/litellm/run.sh` 必须 `exec "$PY" "$DIR/run_proxy.py"` 启动；不要直接 `exec litellm ...`，否则 `input_callback` 的 POST 前修补不会生效。
5. 可选环境变量：`LITELLM_FORCE_PROXY_MODEL`、`VOLCANO_MAX_TOOLS`、`VOLCANO_TOOL_DESC_MAX_CHARS`（写在 `.env` 由 `run.sh` 加载）。
6. 当前主要排障目标已从“wire_api 报错”转向“火山 `InvalidParameter` 兼容性”。

> 建议：把本文当“操作手册 + 历史说明”，具体行为以 `config.yaml` 和 `tool_filter.py` 文件内注释为准（它们已做分段文档化）。

---

## 0. 背景与链路图

```
┌──────────────────┐     Responses API         ┌──────────────────┐     Chat Completions       ┌──────────────────┐
│  Codex Desktop   │ ───────────────────────▶  │   LiteLLM        │ ───────────────────────▶   │  DeepSeek API    │
│  (GUI 客户端)    │  POST /v1/responses        │ 127.0.0.1:4000   │  POST /v1/chat/completions  │ api.deepseek.com │
│                  │ ◀───────────────────────  │  (协议转换)       │ ◀───────────────────────   │                  │
└──────────────────┘     流式 SSE               └──────────────────┘     流式 SSE                └──────────────────┘
```

核心痛点：

- Codex 新版彻底弃用 `wire_api = "chat"`，只保留 Responses API。
- DeepSeek 官方 `api.deepseek.com` 目前只支持 Chat Completions。
- LiteLLM 做双向协议翻译，让 Codex 继续走「原生」协议，底层透明切到 DeepSeek。

---

## 1. 安装 LiteLLM（一次性）

用 `uv tool install` 放进独立的 venv，避免污染系统 Python。

```bash
uv tool install 'litellm[proxy]' --python 3.12
uv tool update-shell        # 把 ~/.local/bin 加进 PATH（写入 ~/.zshenv）
```

验证：

```bash
litellm --version
# 预期：LiteLLM: Current Version = 1.83.x
```

> 如果以后需要升级：`uv tool upgrade litellm`

---

## 2. 创建工作目录

```bash
mkdir -p ~/litellm ~/Library/Logs/LiteLLM
```

- `~/litellm/` 存放 `config.yaml`、`.env`、`run.sh`。
- `~/Library/Logs/LiteLLM/` 存 launchd 重定向过来的 stdout/stderr。

---

## 3. 写 LiteLLM 路由配置 `~/litellm/config.yaml`

### 3.1 参数语义先搞清楚：`context_window` vs `max_tokens` vs `reasoning_effort`

这三个参数**非常容易混淆**，设错就会出问题（比如空回答、费用爆炸、超时）。


| 参数                   | 管什么                                       | 上限                                              | 配在哪里                                                               |
| -------------------- | ----------------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------ |
| **context_window**   | 输入 + 输出 **总和** 的长度上限                      | DeepSeek V4 Pro = **1,048,576** (1M)            | Codex 的 `~/.codex/config.toml` 里的 `model_context_window = 1000000` |
| **max_tokens**       | 单次响应的**输出 token** 上限（不包括输入）               | DeepSeek V4 Pro 实测约 **16K~64K**                 | LiteLLM 的 `~/litellm/config.yaml` 里的 `max_tokens`                  |
| **reasoning_effort** | 思考深度档位，影响 `reasoning_content` 占多少输出 token | `minimal` / `low` / `medium` / `high` / `xhigh` | 两边都能配，Codex 请求里带的会覆盖 LiteLLM 默认                                    |


图示：

```
          ┌──────────── model_context_window = 1,000,000 ────────────┐
          │                                                          │
 请求：    │  [User Message] [Tool Results] [Previous Context] [...]  │
          │        ↑                                                 │
          │    历史全塞进来可以到 ~990K                                │
          │                                                          │
 响应：    │                                         [Assistant Reply]│
          │                                         ↑               │
          │                                         └── max_tokens ──┘
          │                                              最多 16-64K
          └──────────────────────────────────────────────────────────┘
```

> **⛔ 绝对不要把 `max_tokens` 设成 1000000**，那是 context，不是 output。DeepSeek API 会直接拒绝。

### 3.2 reasoning_effort 档位速查表


| 档位        | 思考占比    | 响应速度 | 花费  | 适用场景                 |
| --------- | ------- | ---- | --- | -------------------- |
| `minimal` | ~0%     | 最快   | 便宜  | Chat 问答、简单重构、变量命名    |
| `low`     | ~10-20% | 快    | 低   | 一般编码、写单元测试           |
| `medium`  | ~30-40% | 中等   | 中等  | 多文件重构、bug 定位         |
| `high`    | ~50-60% | 慢    | 高   | 架构设计、复杂算法、长链路 debug  |
| `xhigh`   | ~70%+   | 很慢   | 很高  | 极限深度任务，单次对话 30-60 秒起 |


**搭配规则**：开 `high` 要把 `max_tokens` 给到 ≥ 32K，开 `xhigh` 要给 ≥ 64K，否则思考吃光预算就看到 `content: ""` 空回答。

### 3.3 推荐的分档配置（写入 `~/litellm/config.yaml`）

```yaml
model_list:
  # ────────────── 日常编码主力（推荐默认） ──────────────
  - model_name: deepseek-v4-pro
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_base: https://api.deepseek.com/v1
      api_key: os.environ/DEEPSEEK_API_KEY
      reasoning_effort: "medium"      # 日常够用，平衡速度和质量
      max_tokens: 16384

  # ────────────── 高推理档（架构设计、复杂 bug 定位） ──────────────
  - model_name: deepseek-v4-pro-high
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_base: https://api.deepseek.com/v1
      api_key: os.environ/DEEPSEEK_API_KEY
      reasoning_effort: "high"
      max_tokens: 32768               # 给足思考+输出空间

  # ────────────── 极限档（深度研究、长链路推理） ──────────────
  - model_name: deepseek-v4-pro-max
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_base: https://api.deepseek.com/v1
      api_key: os.environ/DEEPSEEK_API_KEY
      reasoning_effort: "xhigh"
      max_tokens: 65536

  # ────────────── 快速档（Chat、简单任务，不推理） ──────────────
  - model_name: deepseek-v4-pro-fast
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_base: https://api.deepseek.com/v1
      api_key: os.environ/DEEPSEEK_API_KEY
      reasoning_effort: "minimal"
      max_tokens: 4096

  # ────────────── 轻量 Flash（最便宜，永远不推理） ──────────────
  - model_name: deepseek-v4-flash
    litellm_params:
      model: deepseek/deepseek-v4-flash
      api_base: https://api.deepseek.com/v1
      api_key: os.environ/DEEPSEEK_API_KEY
      reasoning_effort: "minimal"
      max_tokens: 4096

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY

litellm_settings:
  drop_params: true          # 丢弃 DeepSeek 不认识的字段
  set_verbose: false
  telemetry: false
  request_timeout: 1200      # 高推理耗时，超时拉长到 20 分钟
  num_retries: 2
```

### 3.4 字段说明


| 字段                            | 作用                                             |
| ----------------------------- | ---------------------------------------------- |
| `model_name`                  | 对 Codex / 客户端暴露的「虚拟模型名」，可以自定义                  |
| `litellm_params.model`        | 真实后端路由，`deepseek/` 前缀走 DeepSeek 驱动             |
| `api_base`                    | DeepSeek Chat Completions 端点（注意带 `/v1`）        |
| `api_key`                     | `os.environ/DEEPSEEK_API_KEY` 表示运行时从环境变量读取     |
| `max_tokens`                  | **输出**上限，LiteLLM 层兜底默认（会被请求里的同名字段覆盖）           |
| `reasoning_effort`            | 思考档位默认，Codex 请求里的 `model_reasoning_effort` 会覆盖 |
| `general_settings.master_key` | LiteLLM 自身的访问鉴权，客户端必须带这个 token                 |
| `drop_params`                 | Codex 偶尔发过来 DeepSeek 不支持的字段，自动丢弃，避免 400        |
| `request_timeout`             | 请求超时秒数，高推理模式必须调大                               |


> **⚠️ DeepSeek V4 Pro 的 thinking 坑（重要）**
>
> V4 Pro 默认走深度推理，会把一大半 `max_tokens` 额度用在 `reasoning_content`（思考过程），留给 `content` 的就很少。如果 `max_tokens` 给小了（比如 64），会看到响应里 `content: ""` 空串、`finish_reason: "length"`、`reasoning_tokens` 把预算全吃光。
>
> **规则**：
>
> - 思考模式 `medium` → `max_tokens ≥ 8K`
> - 思考模式 `high` → `max_tokens ≥ 32K`
> - 思考模式 `xhigh` → `max_tokens ≥ 64K`
> - 简单问答、工具调用头疼 → 用 `deepseek-v4-pro-fast`（`reasoning_effort: "minimal"`）
> - Codex Desktop 会话本身会设很大的 `max_output_tokens`（16K+），所以日常用 `deepseek-v4-pro` 档位没问题，截断的多半是手动 curl 测试

---

## 4. 写密钥文件 `~/litellm/.env`

```bash
# ============================================================
# 1. 把下面的占位符替换成你真实的 DeepSeek API Key
# ============================================================
export DEEPSEEK_API_KEY="sk-REPLACE_ME_WITH_YOUR_DEEPSEEK_KEY"

# ============================================================
# 2. LiteLLM 代理本身的"主密钥"
#    Codex 客户端需要用同一个值作为 Authorization: Bearer
#    只用于本机 127.0.0.1:4000，不出网，可以自定义
# ============================================================
export LITELLM_MASTER_KEY="sk-litellm-local-maerun-2026"
```

**收紧权限，防止泄露：**

```bash
chmod 600 ~/litellm/.env
```

---

## 5. 写启动包装脚本 `~/litellm/run.sh`

launchd 不会帮我们 source `.env`，所以用一个小脚本来做。

```bash
#!/bin/zsh
set -eu

DIR="$HOME/litellm"
cd "$DIR"

# 加载密钥
if [[ -f "$DIR/.env" ]]; then
  set -a
  source "$DIR/.env"
  set +a
fi

# 把 LITELLM_MASTER_KEY 注入 launchd 用户域，
# 这样 Codex Desktop 从 Dock 启动也能读到这个环境变量。
if [[ -n "${LITELLM_MASTER_KEY:-}" ]]; then
  launchctl setenv LITELLM_MASTER_KEY "$LITELLM_MASTER_KEY" 2>/dev/null || true
fi

# 使用 uv 安装的隔离环境
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"

# 必须通过 run_proxy.py 启动，确保 input_callback 注册到 log_pre_api_call
_LIT="$(command -v litellm)"
_PY=""
if [[ -n "$_LIT" && -r "$_LIT" ]]; then
  _PY="$(head -1 "$_LIT")"
  _PY="${_PY#\#!}"
fi
[[ -n "$_PY" && -x "$_PY" ]] || _PY="$(command -v python3)"

exec "$_PY" "$DIR/run_proxy.py" \
  --config "$DIR/config.yaml" \
  --host 127.0.0.1 \
  --port 4000
```

赋予可执行权限：

```bash
chmod 755 ~/litellm/run.sh
```

> 为什么要 `launchctl setenv`？
> macOS GUI 应用从 Dock 启动时**不会**读 `~/.zshrc`，所以 Codex Desktop 读不到普通 shell 环境变量。`launchctl setenv` 写入 launchd 用户域后，之后所有从 Dock 启动的 GUI app 都能拿到。

---

## 6. 写 launchd 定义 `~/Library/LaunchAgents/ai.litellm.proxy.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.litellm.proxy</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/maerun/litellm/run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/maerun/litellm</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>/Users/maerun/Library/Logs/LiteLLM/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/maerun/Library/Logs/LiteLLM/stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/maerun/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>/Users/maerun</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
</dict>
</plist>
```

校验语法：

```bash
plutil -lint ~/Library/LaunchAgents/ai.litellm.proxy.plist
# 预期：...plist: OK
```

关键字段：


| 字段                         | 说明                |
| -------------------------- | ----------------- |
| `RunAtLoad`                | 加载时立即启动           |
| `KeepAlive.Crashed = true` | 崩溃自动重启（正常退出不重启）   |
| `ThrottleInterval = 10`    | 连续重启至少隔 10 秒，防止疯跑 |
| `ProcessType = Background` | 低优先级后台进程，不占前台资源   |


---

## 7. 加载 launchd 服务（一次性）

**必须在系统终端里执行**（Cursor 等 IDE 内置终端可能有沙箱，会报 `Input/output error`）。

```bash
# 首次加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.litellm.proxy.plist

# 立即启动（等不及 RunAtLoad 或卡住了可以强推一下）
launchctl kickstart -k gui/$(id -u)/ai.litellm.proxy
```

验证：

```bash
# 看 state = running, pid = xxx
launchctl print gui/$(id -u)/ai.litellm.proxy | grep -E "state|pid|last exit"

# 看端口监听
lsof -iTCP:4000 -sTCP:LISTEN

# 看日志
tail -f ~/Library/Logs/LiteLLM/stderr.log
```

---

## 8. 把 `LITELLM_MASTER_KEY` 写进 shell rc

让你在普通终端里也能直接调 LiteLLM（curl 测试、Codex CLI 场景）。

```bash
cat >> ~/.zshrc <<'EOF'

# ---------- LiteLLM Proxy (Codex -> DeepSeek) ----------
export LITELLM_MASTER_KEY="sk-litellm-local-maerun-2026"
EOF

source ~/.zshrc
```

---

## 9. 配置 Codex `~/.codex/config.toml`

### 9.1 推荐配置（策略 A：不固定 reasoning，让 GUI 动态控制）

```toml
model = "deepseek-v4-pro"
model_provider = "litellm"
model_context_window = 1000000
# 注意：不固定 model_reasoning_effort，让 Codex Desktop 右下角 GUI 按钮实时切档位
# 如果你想固定一个默认值，取消下一行注释
# model_reasoning_effort = "medium"

sandbox_mode = "workspace-write"
approval_policy = "on-request"

[model_providers.litellm]
name = "LiteLLM (local)"
base_url = "http://127.0.0.1:4000/v1"
env_key = "LITELLM_MASTER_KEY"
wire_api = "responses"
requires_openai_auth = true

# 不同任务场景的 profile，用 `codex --profile xxx` 切换（CLI 场景）
[profiles.deepseek-high]
model = "deepseek-v4-pro-high"
model_provider = "litellm"

[profiles.deepseek-max]
model = "deepseek-v4-pro-max"
model_provider = "litellm"

[profiles.deepseek-fast]
model = "deepseek-v4-pro-fast"
model_provider = "litellm"

[profiles.deepseek-flash]
model = "deepseek-v4-flash"
model_provider = "litellm"
```

### 9.2 关键字段解释

- `model_context_window = 1000000`：告诉 Codex 可以往 prompt 里塞近 1M token 的上下文。**这个是对的**（是 context 不是 output）。
- `wire_api = "responses"`：新版 Codex 强制的协议，LiteLLM 已经替我们兼容好了。
- `requires_openai_auth = true`：**没有这行，Codex Desktop 的 GUI 模型选择器不会显示**（Electron 端已知 Bug）。加上后 App 启动会弹登录框，选「API Key」随便输几个字符回车即可。

### 9.3 两种档位切换策略（二选一或组合）

**策略 A：GUI 动态切（推荐）**

- config.toml 里**不写** `model_reasoning_effort`
- Codex Desktop 右下角 GUI 按钮实时切 `low` / `medium` / `high` / `xhigh`
- LiteLLM 里默认模型是 `deepseek-v4-pro`（`medium`），GUI 切换会覆盖

**策略 B：换 model_name（粗粒度但省心）**

- 在 config.toml 里改 `model = "xxx"`
  - `deepseek-v4-pro` → medium + 16K
  - `deepseek-v4-pro-high` → high + 32K
  - `deepseek-v4-pro-max` → xhigh + 64K
  - `deepseek-v4-pro-fast` → minimal + 4K
- 每次改完 **重启 Codex Desktop** 生效

日常开发建议 **策略 A**，不用改配置。遇到特别复杂的任务临时用 **策略 B** 挂个 `deepseek-v4-pro-max` 会话。

---

## 10. 冒烟测试

```bash
# 1) 模型列表（不涉及 DeepSeek 网络，验证 LiteLLM 本身活着）
curl -s http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool

# 2) Chat Completions（经典协议，用 medium 档位测试）
#    注意 max_tokens 至少给 2048，否则思考吃完就空回答
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "messages": [{"role":"user","content":"你好，一句话自我介绍"}],
    "max_tokens": 2048
  }' | python3 -m json.tool

# 3) Responses API（Codex 用的协议，最关键）
curl -s http://127.0.0.1:4000/v1/responses \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro",
    "input": "用一句话介绍 Rust 语言",
    "max_output_tokens": 2048
  }' | python3 -m json.tool

# 4) 对比：非思考快速档（小 max_tokens 也能秒回）
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-pro-fast",
    "messages": [{"role":"user","content":"你好，一句话自我介绍"}],
    "max_tokens": 128
  }' | python3 -m json.tool
```

**成功判据**：

- Test 1：返回 5 个模型的 JSON 列表
- Test 2：`finish_reason: "stop"` + `content` 有实际文本 + `reasoning_tokens` 合理
- Test 3：`status: "completed"` + `output[].content[].text` 有内容
- Test 4：秒级响应 + `reasoning_tokens: 0` + `content` 有文本

如果 Test 2/3 返回 `content: ""` 而 `reasoning_tokens` 爆满，说明 `max_tokens` 太小被思考吃光（而不是 Key 或网络问题）。

---

## 11. 启动 Codex Desktop

1. 完全退出（`Cmd+Q`，不是关窗口）。
2. 从 Dock 重新打开。
3. 弹登录框 → 选 API Key → 随便输几个字符回车。
4. 右下角模型选择器能看到 5 个档位：
  - `deepseek-v4-pro`（默认 medium，日常）
  - `deepseek-v4-pro-high`（复杂任务）
  - `deepseek-v4-pro-max`（极限推理）
  - `deepseek-v4-pro-fast`（Chat / 简单）
  - `deepseek-v4-flash`（最便宜）
5. 旁边的推理强度按钮可以在 `low / medium / high / xhigh` 间实时切换（策略 A 模式下生效）。

### 日常选档建议


| 任务                    | 推荐模型                   | 推荐 effort |
| --------------------- | ---------------------- | --------- |
| 一般编码 / 小范围重构          | `deepseek-v4-pro`      | `medium`  |
| 多文件改造 / bug 定位        | `deepseek-v4-pro`      | `high`    |
| 架构设计 / 深度代码 review    | `deepseek-v4-pro-high` | `high`    |
| 长链路推理 / monorepo 全局分析 | `deepseek-v4-pro-max`  | `xhigh`   |
| 代码格式化 / 重命名 / 加注释     | `deepseek-v4-pro-fast` | `minimal` |
| Chat 闲聊 / 快速问答        | `deepseek-v4-flash`    | `minimal` |


---

# 日常使用命令速查

## 运行状态检查

```bash
# 服务是否在跑
launchctl print gui/$(id -u)/ai.litellm.proxy | grep -E "state|pid|last exit"

# 端口监听确认
lsof -iTCP:4000 -sTCP:LISTEN

# 进程详情
ps -ef | grep -v grep | grep litellm
```

## 日志查看

```bash
# 实时跟踪
tail -f ~/Library/Logs/LiteLLM/stderr.log

# 看最近 200 行
tail -200 ~/Library/Logs/LiteLLM/stderr.log

# 清空日志（太大时）
: > ~/Library/Logs/LiteLLM/stderr.log
: > ~/Library/Logs/LiteLLM/stdout.log
```

## 重启 / 启停

```bash
# 改完 config.yaml 或 .env 后重启
launchctl kickstart -k gui/$(id -u)/ai.litellm.proxy

# 临时停止
launchctl bootout gui/$(id -u)/ai.litellm.proxy

# 重新加载（停掉后重新启用）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.litellm.proxy.plist

# 永久禁用（不会开机启动）
launchctl disable gui/$(id -u)/ai.litellm.proxy
# 恢复
launchctl enable gui/$(id -u)/ai.litellm.proxy
```

## 升级 / 卸载

```bash
# 升级到最新版
uv tool upgrade litellm
launchctl kickstart -k gui/$(id -u)/ai.litellm.proxy

# 彻底卸载（保留配置文件）
launchctl bootout gui/$(id -u)/ai.litellm.proxy
rm ~/Library/LaunchAgents/ai.litellm.proxy.plist
uv tool uninstall litellm

# 连配置一起删
rm -rf ~/litellm ~/Library/Logs/LiteLLM
```

## 手动前台运行（调试用）

有时出问题想看实时详细输出，先把 launchd 版本停掉：

```bash
launchctl bootout gui/$(id -u)/ai.litellm.proxy
source ~/litellm/.env
python3 ~/litellm/run_proxy.py --config ~/litellm/config.yaml --host 127.0.0.1 --port 4000 --detailed_debug
```

`Ctrl+C` 退出后再 `launchctl bootstrap ...` 恢复后台模式。

## 换 / 加模型

编辑 `~/litellm/config.yaml`，在 `model_list` 下追加一个 entry，保存后：

```bash
launchctl kickstart -k gui/$(id -u)/ai.litellm.proxy
```

常用模型示例：

```yaml
  # Claude 官方
  - model_name: claude-4.6-sonnet
    litellm_params:
      model: anthropic/claude-4.6-sonnet
      api_key: os.environ/ANTHROPIC_API_KEY

  # OpenAI 官方
  - model_name: gpt-5.5
    litellm_params:
      model: openai/gpt-5.5
      api_key: os.environ/OPENAI_API_KEY

  # Google Gemini
  - model_name: gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: os.environ/GEMINI_API_KEY

  # 本地 Ollama
  - model_name: qwen3-coder-local
    litellm_params:
      model: ollama/qwen3-coder
      api_base: http://localhost:11434
```

对应的 API Key 在 `~/litellm/.env` 里追加 `export` 即可。

## 换 DeepSeek Key

```bash
# 直接编辑
open -e ~/litellm/.env
# 或 sed 替换（谨慎）
sed -i '' 's|sk-旧Key|sk-新Key|' ~/litellm/.env

# 重启生效
launchctl kickstart -k gui/$(id -u)/ai.litellm.proxy
```

---

# 常见报错排查


| 报错                                                                  | 原因                              | 解决                                                                                   |
| ------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------ |
| Codex 启动提示 `wire_api = "chat" is no longer supported`               | 老配置没改掉                          | 按第 9 步把 `wire_api` 改成 `"responses"`，`base_url` 改到 `127.0.0.1:4000/v1`                |
| `launchctl bootstrap` 报 `Input/output error (5)`                    | 在 IDE 的沙箱终端里跑                   | 换到系统原生终端（Terminal.app / iTerm）里执行                                                    |
| `Not privileged to set domain environment`                          | 同上，沙箱限制                         | 系统终端里执行 `launchctl setenv`                                                           |
| curl 返回 `401 Unauthorized` 来自 LiteLLM                               | 客户端没带或带错了 `LITELLM_MASTER_KEY`  | 确认 `~/litellm/.env` 和 `~/.zshrc` 里的值一致                                               |
| curl 返回 `401` 但信息带 DeepSeek 字样                                      | DeepSeek API Key 错了             | 改 `~/litellm/.env` 里的 `DEEPSEEK_API_KEY`，重启服务                                        |
| Codex Desktop 没模型选择器                                                | 缺 `requires_openai_auth = true` | 按第 9 步补上并重启 App                                                                      |
| Codex 发请求一直超时                                                       | launchd 没跑 / 端口被占               | `lsof -iTCP:4000` 查一下；`launchctl print ...` 看状态                                      |
| 4000 端口被别的进程占了                                                      | 有其他程序抢了                         | 改 `run.sh` 和 `config.toml` 里的端口为 `4001` 等；或 `kill -9 <pid>`                          |
| 长回复中途断流                                                             | 默认超时不够                          | `config.yaml` 里 `request_timeout` 调大                                                 |
| DeepSeek 返回 `insufficient tool messages`                            | 工具调用时 reasoning 模式参数不兼容         | 确认用的是 `deepseek-v4-pro` 非 reasoner 变体；`drop_params: true` 已开                         |
| `content: ""` 空回答 + `finish_reason: length` + `reasoning_tokens` 爆满 | thinking 模式把 `max_tokens` 额度吃光  | 把 `max_tokens` 加到 ≥ 2048；或改用 `deepseek-v4-pro-fast` 档（`reasoning_effort: "minimal"`） |
| `litellm.BadRequestError ... InvalidParameter ... model group=kimi-k2.6` | 火山 OpenAI 兼容层不接受请求体某些结构（常见于 tools/schema 过大） | 检查 `stdout.log` 的 `[volcano_tool_slim]`、`[volcano_ark_sanitize]`；按需调 `VOLCANO_MAX_TOOLS` 与 `VOLCANO_TOOL_DESC_MAX_CHARS` |
| curl 最小请求成功，但 Codex Desktop `say hi` 仍 400 | Codex 实际会自动携带多条 messages + 多个 tools，不是最小 payload | 以 `/v1/responses` 日志计数为准，重点看 `tool_filter` 与 `volcano_*` 前缀日志 |


---

# 架构小结

- **Codex Desktop 侧**：只需认准 `http://127.0.0.1:4000/v1`，协议走 Responses API。
- **LiteLLM 侧**：做协议转换 + 多供应商路由 + 统一鉴权，是整个体系的"电源适配器"。
- **DeepSeek 侧**：只暴露 Chat Completions，LiteLLM 替我们兼容。
- **launchd 侧**：开机自启 + 崩溃重拉 + 日志管理，不需要你 babysit。

后续要接 Claude / GPT / Gemini 时，**只改 `~/litellm/config.yaml` 一个文件**，所有客户端（Codex、Cursor、Cline、Continue、自己写的脚本）统一通过 LiteLLM 走，模型切换零摩擦。

---

# 参数决策快速回顾

**踩过一次坑后的黄金 3 条规则**：

1. `**context_window = 1M` ≠ `max_tokens = 1M`**
  - Context 指输入+输出总和，Codex 侧填 `1000000`
  - Max_tokens 只管输出，DeepSeek 最多 16K~64K，LiteLLM 侧填 `16384` / `32768` / `65536`
2. `**reasoning_effort` 档位越高，`max_tokens` 要越大**
  - 比例大约：`medium → 16K`、`high → 32K`、`xhigh → 64K`
  - 预算不够会看到 `content: ""` 空回答 + `reasoning_tokens` 爆表
3. **Codex Desktop GUI 右下角按钮能动态改 `reasoning_effort`**
  - 不用为了切档位反复改配置
  - 配置里只写模型 + 上下文窗口，推理强度让 UI 控制

---

# 关键文件清单


| 路径                                              | 作用                                               | 权限      |
| ----------------------------------------------- | ------------------------------------------------ | ------- |
| `~/litellm/config.yaml`                         | LiteLLM 路由配置                                     | 644     |
| `~/litellm/.env`                                | DeepSeek Key + LiteLLM 主密钥                       | **600** |
| `~/litellm/run.sh`                              | launchd 启动脚本                                     | 755     |
| `~/Library/LaunchAgents/ai.litellm.proxy.plist` | launchd 服务定义                                     | 644     |
| `~/Library/Logs/LiteLLM/stdout.log`             | 正常输出日志                                           | —       |
| `~/Library/Logs/LiteLLM/stderr.log`             | 错误日志                                             | —       |
| `~/.codex/config.toml`                          | Codex 客户端指向 LiteLLM                              | 644     |
| `~/.zshrc`（末尾）                                  | 导出 `LITELLM_MASTER_KEY`                          | —       |
| `~/.zshenv`                                     | `~/.local/bin` 进 PATH（`uv tool update-shell` 写入） | —       |


---

> 最后更新：2026-05-04（补火山方舟/Kimi 路径、run_proxy 启动要求、tool_filter 兼容修补说明）
> 维护人：maerun

