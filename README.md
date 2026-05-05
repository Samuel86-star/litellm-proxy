# litellm-proxy（macOS）

这个仓库用于在 macOS 上快速复现你的 LiteLLM 本地代理方案（Codex/Cursor 等客户端统一走本机 `127.0.0.1:4000`）。

## 仓库结构

- `run_proxy.py`：启动包装，确保 `input_callback` 注册生效
- `tool_filter.py`：请求兼容修补（DeepSeek + 火山方舟）
- `config.example.yaml`：LiteLLM 配置模板
- `.env.example`：环境变量模板（含可选 Volcano 调优项）
- `litellmctl.sh`：常用运维命令脚本（init/bootstrap/reload/status/logs/test/doctor）
- `docs/LiteLLM.md`：完整操作手册（详细版）

## 在新 Mac 上的最短落地步骤

### 1) 安装依赖

```bash
brew install uv
uv tool install 'litellm[proxy]' --python 3.12
uv tool update-shell
```

### 2) 初始化本地目录（推荐）

```bash
chmod +x ./litellmctl.sh
./litellmctl.sh init
```

### 3) 写配置文件（按文档模板）

按 `docs/LiteLLM.md` 写入以下文件：

- `~/litellm/config.yaml`
- `~/litellm/.env`
- `~/litellm/run.sh`
- `~/Library/LaunchAgents/ai.litellm.proxy.plist`

> 说明：`config.yaml` 和 `.env` 里必须填写你自己的密钥；`run.sh` 必须走 `run_proxy.py`，不要直接 `exec litellm`。

### 4) 启动服务

```bash
./litellmctl.sh bootstrap
```

### 5) 验证

```bash
./litellmctl.sh test
```

## 高频命令（推荐用脚本）

```bash
# 首次初始化（仅在目标文件不存在时复制模板，不覆盖你现有文件）
./litellmctl.sh init

# 改完 ~/litellm/config.yaml 或 ~/litellm/.env 后重载
./litellmctl.sh reload

# 看状态（launchd + 4000 端口）
./litellmctl.sh status

# 跟日志
./litellmctl.sh logs

# 综合检查
./litellmctl.sh doctor
```

## 给 AI 工具的执行指令（可直接复制）

如果你在新机器上让 Cursor/Claude/Codex 代你搭环境，可以直接给它这段：

```text
请基于当前仓库完成 macOS LiteLLM 本地代理搭建：
1) 安装 uv + litellm[proxy]
2) 将仓库里的 run_proxy.py / tool_filter.py 复制到 ~/litellm/
3) 按 docs/LiteLLM.md 生成 ~/litellm/config.yaml、.env、run.sh 与 launchd plist
4) 用 litellmctl.sh bootstrap 启动，并完成 litellmctl.sh test 冒烟测试
5) 输出最终检查结果（端口监听、launchctl 状态、测试响应）
```

## 备注

- 当前方案不依赖 Docker。
- 如果后续要提交此仓库，建议先检查是否误提交任何真实密钥。
