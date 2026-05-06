#!/bin/zsh
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$HOME/litellm"
LOG_DIR="$HOME/Library/Logs/LiteLLM"
PLIST="$HOME/Library/LaunchAgents/ai.litellm.proxy.plist"

SERVICE_LABEL="ai.litellm.proxy"
DOMAIN="gui/$(id -u)"

echo "========== LiteLLM Proxy 安装脚本 =========="
echo "项目目录: $REPO_DIR"
echo "运行目录: $TARGET_DIR"
echo ""

# 1. 确保 litellm 已安装
echo "[1/6] 检查 litellm 安装..."
if ! command -v litellm &>/dev/null; then
  echo "ERROR: litellm 未安装"
  echo "请先执行: uv tool install 'litellm[proxy]' --python 3.12"
  exit 1
fi
echo "  OK: $(litellm --version)"

# 2. 创建运行目录
echo "[2/6] 创建运行目录..."
mkdir -p "$TARGET_DIR" "$LOG_DIR"

# 3. 复制运行所需的文件（仅在目标不存在时复制，不覆盖你的配置）
echo "[3/6] 复制运行时文件到 $TARGET_DIR ..."

copy_if_missing() {
  local src="$1"
  local dst="$2"
  if [[ -f "$dst" ]]; then
    echo "  SKIP: $dst 已存在"
  else
    cp "$src" "$dst"
    echo "  COPY: $dst"
  fi
}

copy_if_missing "$REPO_DIR/run_proxy.py"        "$TARGET_DIR/run_proxy.py"
copy_if_missing "$REPO_DIR/tool_filter.py"      "$TARGET_DIR/tool_filter.py"
copy_if_missing "$REPO_DIR/config.example.yaml" "$TARGET_DIR/config.yaml"
copy_if_missing "$REPO_DIR/.env.example"        "$TARGET_DIR/.env"

# run.sh 和运维脚本每次复制最新版本（它们是"程序"不是"配置"）
cp "$REPO_DIR/run.sh" "$TARGET_DIR/run.sh"
chmod +x "$TARGET_DIR/run.sh"
echo "  COPY: $TARGET_DIR/run.sh (最新版)"

cp "$REPO_DIR/litellmctl.sh" "$TARGET_DIR/litellmctl.sh"
chmod +x "$TARGET_DIR/litellmctl.sh"
echo "  COPY: $TARGET_DIR/litellmctl.sh (最新版)"

# .env 权限收紧
chmod 600 "$TARGET_DIR/.env" 2>/dev/null || true

# 4. 生成 plist
echo "[4/6] 生成 launchd plist..."
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$SERVICE_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$TARGET_DIR/run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$TARGET_DIR</string>

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
    <string>$LOG_DIR/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
</dict>
</plist>
EOF
plutil -lint "$PLIST"

# 5. 启动服务
echo "[5/6] 启动 launchd 服务..."
launchctl bootout "$DOMAIN/$SERVICE_LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 2
launchctl kickstart -k "$DOMAIN/$SERVICE_LABEL"
sleep 3

# 6. 验证
echo "[6/6] 验证状态..."
echo ""
echo "launchd 状态:"
launchctl print "$DOMAIN/$SERVICE_LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "  无法获取状态"

echo ""
echo "端口监听:"
lsof -iTCP:4000 -sTCP:LISTEN 2>/dev/null || echo "  端口 4000 无监听"

echo ""
echo "最近 stderr 日志:"
tail -20 "$LOG_DIR/stderr.log" 2>/dev/null || echo "  无日志"

echo ""
echo "========== 安装完成 =========="
echo ""
echo "后续操作（在任意目录都可以）:"
echo "  ~/litellm/litellmctl.sh status    # 看状态"
echo "  ~/litellm/litellmctl.sh test      # 冒烟测试"
echo "  ~/litellm/litellmctl.sh logs      # 看日志"
echo "  ~/litellm/litellmctl.sh reload    # 重启"
echo ""
echo "如果你还没填密钥，请编辑:"
echo "  open -e ~/litellm/.env"
