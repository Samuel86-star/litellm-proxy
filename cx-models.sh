# =============================================================================
# Codex Desktop 模型切换启动器
# 用法: cx          -> 列出可用模型
#       cx <model>  -> 切换模型并启动 Codex Desktop
#       cx --kill   -> 仅杀掉 Codex Desktop
#
# 安装: 把本文件 content 追加到 ~/.zshrc 末尾
#   cat ~/Documents/litellm-proxy/cx-models.sh >> ~/.zshrc
#   source ~/.zshrc
# =============================================================================

# 可用模型列表（与 ~/litellm/config.yaml 保持同步）
_CX_MODELS=(
  deepseek-v4-pro
  deepseek-v4-flash
  glm-5.1
  kimi-k2.6
  minimax-m2.7
  doubao-seed-code
  ark-auto
  gpt-5.4
  gpt-5.5
  gpt-5.4-mini
  gpt-5.3-codex
  gpt-5.2
)

cx() {
  local model="${1:-}"
  local config="$HOME/.codex/config.toml"
  local app="/Applications/Codex.app"

  # --------------- 无参数：列出可用模型 ---------------
  if [[ -z "$model" ]]; then
    echo "Usage: cx <model>"
    echo ""
    echo "Available models:"
    echo "  DeepSeek:"
    echo "    deepseek-v4-pro    deepseek-v4-flash"
    echo "  Volcano Ark:"
    echo "    glm-5.1            kimi-k2.6           minimax-m2.7"
    echo "    doubao-seed-code   ark-auto"
    echo "  Aliases (→ kimi-k2.6):"
    echo "    gpt-5.4            gpt-5.5             gpt-5.4-mini"
    echo "    gpt-5.3-codex      gpt-5.2"
    return 0
  fi

  # --------------- --kill：仅杀掉进程 ---------------
  if [[ "$model" == "--kill" ]]; then
    pkill -f "Codex.app" 2>/dev/null && echo "Codex killed." || echo "Codex not running."
    return 0
  fi

  # --------------- 验证模型名 ---------------
  local valid=0
  for m in $_CX_MODELS; do
    [[ "$m" == "$model" ]] && valid=1 && break
  done
  if [[ $valid -eq 0 ]]; then
    echo "Unknown model: $model"
    echo "Run 'cx' with no args to see available models."
    return 1
  fi

  # --------------- 修改 config.toml ---------------
  if [[ ! -f "$config" ]]; then
    echo "ERROR: $config not found."
    return 1
  fi

  # macOS sed 语法
  if grep -q '^model ' "$config" || grep -q '^model=' "$config"; then
    sed -i '' "s/^model *= *.*/model = \"$model\"/" "$config"
  else
    # 没有 model 行，插入到第一行之后
    sed -i '' "1a\\
model = \"$model\"
" "$config"
  fi
  echo "config.toml: model = \"$model\""

  # --------------- 杀掉旧进程 ---------------
  pkill -f "Codex.app" 2>/dev/null && sleep 2

  # --------------- 启动 Codex Desktop ---------------
  if [[ -d "$app" ]]; then
    open "$app"
  else
    # 尝试其他路径
    local alt=$(mdfind "kMDItemKind == 'Application'" | grep -i "codex" | head -1)
    if [[ -n "$alt" ]]; then
      open "$alt"
    else
      echo "WARNING: Codex Desktop.app not found. Please start manually."
      return 1
    fi
  fi

  echo "Codex Desktop starting with model: $model"
}

# --------------- Tab 补全 ---------------
_cx_completions() {
  local context="$words[CURRENT-1]"
  if [[ "$context" =~ ^(-?-) ]]; then
    compadd -- "--kill"
  fi
  compadd -- $_CX_MODELS
}
compdef _cx_completions cx 2>/dev/null || true
