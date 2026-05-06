# litellm-proxy（macOS）

在 macOS 上用 LiteLLM 搭建本地代理，将 OpenAI 协议转换为 DeepSeek 和火山方舟 Coding Plan 兼容格式。Codex/Cursor 等客户端统一走本机 `127.0.0.1:4000`。

## 目录区分

| 目录 | 用途 |
|------|------|
| `~/Documents/litellm-proxy/`（本项目） | Git 仓库，放源码模板和安装脚本 |
| `~/litellm/`（运行目录） | 实际运行配置、密钥文件和运维脚本 |

运行目录由 `install.sh` 自动生成，日常运维只需要记住 `~/litellm/litellmctl.sh`。

## 仓库结构

- `run_proxy.py`：启动包装，确保 `input_callback` 注册生效（必须在 `run.sh` 中通过 uv 的 Python 执行）
- `tool_filter.py`：请求兼容修补（DeepSeek V4 reasoning + 火山方舟参数剥离 / schema 放松 / tools 限额）
- `config.example.yaml`：LiteLLM 路由配置模板（DeepSeek + Volcano 全模型）
- `.env.example`：环境变量模板（含可选 Volcano 调优项）
- `run.sh`：启动脚本（自动找到 uv 隔离环境里的 Python 来加载 litellm 模块）
- `litellmctl.sh`：运维脚本（status/logs/test/reload）
- `install.sh`：**一键安装脚本**，复制文件、生成 plist、启动服务
- `docs/LiteLLM.md`：完整操作手册（详细版）

## 在新 Mac 上的最短落地步骤

### 1) 安装依赖

```bash
brew install uv
uv tool install 'litellm[proxy]' --python 3.12
uv tool update-shell
```

### 2) 克隆仓库并运行安装脚本

```bash
cd ~/Documents
# 克隆你的仓库（替换成你的仓库地址）
git clone <your-repo-url> litellm-proxy
cd litellm-proxy
chmod +x install.sh
./install.sh
```

`install.sh` 会自动完成：创建 `~/litellm/`、复制配置文件、生成正确的 launchd plist、启动服务。

### 3) 填入真实密钥

```bash
open -e ~/litellm/.env
```

替换以下三个值：
- `DEEPSEEK_API_KEY`
- `VOLCANO_ARK_API_KEY`
- `LITELLM_MASTER_KEY`（任意字符串，客户端连接时代理鉴权用）

### 4) 重启服务使配置生效

```bash
~/litellm/litellmctl.sh reload
```

### 5) 验证

```bash
~/litellm/litellmctl.sh test
```

预期返回 12 个模型的 JSON 列表（DeepSeek 2 个 + 火山 5 个 + Codex 别名 5 个）。

## 高频命令（日常运维）

```bash
# 看状态（launchd + 4000 端口）
~/litellm/litellmctl.sh status

# 改完 config.yaml 或 .env 后重载
~/litellm/litellmctl.sh reload

# 跟日志
~/litellm/litellmctl.sh logs

# 冒烟测试
~/litellm/litellmctl.sh test

# 综合检查
~/litellm/litellmctl.sh doctor

# 临时停止
~/litellm/litellmctl.sh stop
```

## 常见问题

**服务启动后端口无监听、日志为空？**
- 检查 plist 路径是否正确：`grep REPLACE ~/Library/LaunchAgents/ai.litellm.proxy.plist`，如果有命中说明安装脚本没跑
- 检查 `~/litellm/run.sh` 是否存在且可执行：`ls -la ~/litellm/run.sh`
- 检查 litellm 是否通过 uv 安装：`~/.local/bin/litellm --version`

**日志显示 `ModuleNotFoundError: No module named 'litellm'`？**
- `run.sh` 必须用 uv 管理的 Python 执行 `run_proxy.py`，不能直接调用系统 `/usr/bin/python3`
- 运行 `install.sh` 或 `cp run.sh ~/litellm/run.sh && chmod +x ~/litellm/run.sh` 即可修复

**Codex Desktop 弹登录框？**
- 这是正常的，`requires_openai_auth = true` 必须开启才会显示模型选择器
- 选 "API Key"，随便输入几个字符回车即可

## 给 AI 工具的执行指令（可直接复制）

如果你在新机器上让 Cursor/Claude/Codex 代你搭环境，直接给它这段：

```text
请基于当前仓库完成 macOS LiteLLM 本地代理搭建：
1) 安装 uv + litellm[proxy]（brew install uv; uv tool install 'litellm[proxy]' --python 3.12）
2) 运行 ./install.sh 一键安装（会自动创建 ~/litellm/、复制文件、生成 plist、启动服务）
3) 让用户编辑 ~/litellm/.env 填入 DEEPSEEK_API_KEY、VOLCANO_ARK_API_KEY、LITELLM_MASTER_KEY
4) 运行 ~/litellm/litellmctl.sh reload 使配置生效
5) 运行 ~/litellm/litellmctl.sh test 冒烟测试
6) 输出最终检查结果（端口监听、launchctl 状态、测试响应）
```

## 备注

- 当前方案不依赖 Docker。
- 不要提交真实密钥到仓库，`config.yaml` 和 `.env` 的运行时副本也不应提交（已由 .gitignore 排除）。
- 如需新增模型，编辑 `config.example.yaml`，然后重新运行 `./install.sh` 或手动同步到 `~/litellm/config.yaml`。
