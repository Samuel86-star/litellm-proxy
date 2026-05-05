#!/usr/bin/env python3
"""
LiteLLM 代理启动包装：在 run_server() 之前注册 input_callback。

为何单独一个文件
    config.yaml 里 litellm_settings.callbacks 只会进入 litellm.callbacks，
    不会进入 litellm.input_callback，因此 CustomLogger.log_pre_api_call 默认不执行。
    DeepSeek V4 的 reasoning_content、火山 POST 前最后一轮清洗等，都依赖
    log_pre_api_call 里的 complete_input_dict（见 tool_filter.DeepSeekV4ReasoningFix）。

启动方式
    run.sh 应 exec 本脚本而非直接 litellm，否则 input_callback 未注册。
"""
from __future__ import annotations

import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)


def main() -> None:
    import litellm
    import tool_filter

    litellm.logging_callback_manager.add_litellm_input_callback(
        tool_filter.deepseek_v4_reasoning_fix
    )

    from litellm import run_server

    run_server()


if __name__ == "__main__":
    main()
