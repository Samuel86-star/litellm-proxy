#!/bin/zsh
set -eu

SERVICE_LABEL="ai.litellm.proxy"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/${SERVICE_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/LiteLLM"
ENV_FILE="$HOME/litellm/.env"
TARGET_DIR="$HOME/litellm"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

usage() {
  cat <<'EOF'
Usage: ./litellmctl.sh <command>

Commands:
  init        Create ~/litellm and copy template files if missing
  bootstrap   Load launchd plist and start service
  reload      Restart running service (after config.yaml/.env changes)
  status      Show launchd state + port 4000 listener
  logs        Tail LiteLLM stderr log
  stop        Stop and unload service
  test        Call /v1/models smoke test
  doctor      Run status + test + recent error log
EOF
}

check_files() {
  if [[ ! -f "$PLIST" ]]; then
    echo "Missing plist: $PLIST"
    exit 1
  fi
}

cmd="${1:-}"
if [[ -z "$cmd" ]]; then
  usage
  exit 1
fi

case "$cmd" in
  init)
    mkdir -p "$TARGET_DIR" "$LOG_DIR"
    [[ -f "$TARGET_DIR/run_proxy.py" ]] || cp "$REPO_DIR/run_proxy.py" "$TARGET_DIR/run_proxy.py"
    [[ -f "$TARGET_DIR/tool_filter.py" ]] || cp "$REPO_DIR/tool_filter.py" "$TARGET_DIR/tool_filter.py"
    [[ -f "$TARGET_DIR/config.yaml" ]] || cp "$REPO_DIR/config.example.yaml" "$TARGET_DIR/config.yaml"
    [[ -f "$TARGET_DIR/.env" ]] || cp "$REPO_DIR/.env.example" "$TARGET_DIR/.env"
    chmod 600 "$TARGET_DIR/.env" 2>/dev/null || true
    echo "Init done."
    echo "Next:"
    echo "  1) Edit $TARGET_DIR/.env and $TARGET_DIR/config.yaml"
    echo "  2) Ensure ~/Library/LaunchAgents/${SERVICE_LABEL}.plist exists"
    echo "  3) Run: ./litellmctl.sh bootstrap"
    ;;
  bootstrap)
    check_files
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
    launchctl kickstart -k "${DOMAIN}/${SERVICE_LABEL}"
    ;;
  reload)
    launchctl kickstart -k "${DOMAIN}/${SERVICE_LABEL}"
    ;;
  status)
    launchctl print "${DOMAIN}/${SERVICE_LABEL}" | rg "state|pid|last exit" || true
    echo "---"
    lsof -iTCP:4000 -sTCP:LISTEN || true
    ;;
  logs)
    mkdir -p "$LOG_DIR"
    touch "$LOG_DIR/stderr.log"
    tail -f "$LOG_DIR/stderr.log"
    ;;
  stop)
    launchctl bootout "${DOMAIN}/${SERVICE_LABEL}" || true
    ;;
  test)
    if [[ -f "$ENV_FILE" ]]; then
      set -a
      source "$ENV_FILE"
      set +a
    fi
    if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
      echo "LITELLM_MASTER_KEY is empty. Check $ENV_FILE."
      exit 1
    fi
    curl -sS "http://127.0.0.1:4000/v1/models" \
      -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" | python3 -m json.tool
    ;;
  doctor)
    "$0" status
    echo "==="
    "$0" test || true
    echo "==="
    if [[ -f "$LOG_DIR/stderr.log" ]]; then
      tail -n 80 "$LOG_DIR/stderr.log"
    else
      echo "No stderr.log yet: $LOG_DIR/stderr.log"
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
