#!/usr/bin/env bash
set -eu

DIR="$HOME/litellm"
cd "$DIR"

# 加载密钥
if [[ -f "$DIR/.env" ]]; then
  set -a
  source "$DIR/.env"
  set +a
fi

# macOS: 把 LITELLM_MASTER_KEY 注入 launchd 用户域
if [[ "$OSTYPE" == "darwin"* && -n "${LITELLM_MASTER_KEY:-}" ]]; then
  launchctl setenv LITELLM_MASTER_KEY "$LITELLM_MASTER_KEY" 2>/dev/null || true
fi

# 使用 uv 安装的隔离环境（必须放前面，否则找到的是系统 python）
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

# 找到 uv 安装的 litellm 入口（它有正确的 shebang 指向 uv 管理的 Python）
LITELLM_BIN="$HOME/.local/bin/litellm"
if [[ ! -x "$LITELLM_BIN" ]]; then
  echo "ERROR: litellm not found at $LITELLM_BIN"
  echo "Please run: uv tool install 'litellm[proxy]' --python 3.12"
  exit 1
fi

# 通过 litellm 的 shebang 找到 uv 管理的 Python 解释器
# litellm 脚本第一行形如: #!/Users/xxx/.local/share/uv/tools/litellm/bin/python3
_PY="$(head -1 "$LITELLM_BIN")"
_PY="${_PY#\#!}"

# 兜底：如果 shebang 解析失败，尝试 uv 工具目录下的 python3
if [[ ! -x "$_PY" ]]; then
  _PY="$(find "$HOME/.local/share/uv/tools/litellm/bin" -name 'python*' -type f -executable | head -1)"
fi

if [[ ! -x "$_PY" ]]; then
  echo "ERROR: Cannot find uv-managed Python interpreter"
  exit 1
fi

# 通过 uv 的 Python 执行 run_proxy.py，这样 litellm 模块才能被找到
exec "$_PY" "$DIR/run_proxy.py" \
  --config "$DIR/config.yaml" \
  --host 127.0.0.1 \
  --port 4000
