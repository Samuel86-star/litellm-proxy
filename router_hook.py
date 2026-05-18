#!/usr/bin/env python3
"""
LiteLLM 智能路由器 —— 根据 prompt 关键词 + Kimi 评分动态分配模型。

路由优先级（从高到低）：
  P0: OCR 关键词 → kimi-k2.6
  P1: DeepSeek 关键词 → deepseek-v4-pro
  P2: Kimi 四维评分 ≥ ROUTER_SCORE_THRESHOLD → deepseek-v4-pro
  P3: 兜底 → kimi-k2.6
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Optional

from litellm.integrations.custom_logger import CustomLogger


def _get_env_list(key: str, default: str) -> list[str]:
    raw = os.environ.get(key, default).strip()
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


class SmartRouter(CustomLogger):
    def __init__(self):
        super().__init__()
        self.ocr_keywords = _get_env_list(
            "ROUTER_OCR_KEYWORDS",
            "ocr,图片识别,图像识别,图文,截图,扫描,文字识别",
        )
        self.ds_keywords = _get_env_list(
            "ROUTER_DEEPSEEK_KEYWORDS",
            "重构,设计,架构,optimize,debug,refactor,architect,写方案,提供方案",
        )
        self.score_threshold = _get_env_int("ROUTER_SCORE_THRESHOLD", 20)
        self.cache_ttl = _get_env_int("ROUTER_SCORE_CACHE_TTL", 300)
        self.log_level = os.environ.get("ROUTER_LOG_LEVEL", "info")
        self.scoring_temperature = _get_env_float(
            "ROUTER_SCORING_TEMPERATURE", 0.0
        )
        self.scoring_timeout = _get_env_int("ROUTER_SCORING_TIMEOUT", 10)
        self.scoring_system_prompt = os.environ.get("ROUTER_SCORING_SYSTEM_PROMPT") or (
            "你是一个任务复杂度评估器。对以下任务按四个维度打分（各 1-5 分）：\n"
            "1. 推理深度：逻辑推断、多步推理、抽象思考的强度\n"
            "2. 代码量：预期生成的代码行数/文件数\n"
            "3. 领域知识：是否需要特定框架、系统、算法的深度知识\n"
            "4. 上下文长度：是否需要理解大量已有代码/文档\n"
            "只返回 JSON：{\"reasoning\":N,\"code_volume\":N,\"domain\":N,\"context_length\":N}"
        )
        self._score_cache: dict[str, tuple[float, int]] = {}  # fingerprint → (timestamp, score)

    def _log(self, msg: str, level: str = "info") -> None:
        if level == "debug" and self.log_level != "debug":
            return
        print(f"[SmartRouter] {msg}", flush=True)


router_hook = SmartRouter()


# --- 路由逻辑 ----------------------------------------------------------------

def _extract_user_prompt(data: dict) -> str:
    """从请求中提取最后一条 user 消息的文本。"""
    messages = data.get("messages") or data.get("input")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict) and p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
            return " ".join(parts)
        return ""
    return ""


def _match_any_keyword(text: str, keywords: list[str]) -> bool:
    """不区分大小写的关键词匹配。"""
    if not text or not keywords:
        return False
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _compute_fingerprint(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


async def _kimi_score_prompt(prompt: str, router: SmartRouter) -> float:
    """调用 kimi-k2.6 对 prompt 四维打分，带缓存。"""
    import litellm

    fingerprint = _compute_fingerprint(prompt)
    cached = router._score_cache.get(fingerprint)
    now = time.time()
    if cached is not None and (now - cached[1]) < router.cache_ttl:
        router._log(f"score cache hit fingerprint={fingerprint[:8]} score={cached[0]}", "debug")
        return cached[0]

    router._log(f"calling kimi-k2.6 scoring prompt_len={len(prompt)}", "debug")
    try:
        resp = await litellm.acompletion(
            model="kimi-k2.6",
            messages=[
                {"role": "system", "content": router.scoring_system_prompt},
                {"role": "user", "content": prompt[:4000]},
            ],
            temperature=router.scoring_temperature,
            timeout=router.scoring_timeout,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
        raw = raw.strip()
        # 容忍 ```json ``` 包裹
        raw = re.sub(r"^\s*```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
        parsed = json.loads(raw)
        score = float(
            parsed.get("reasoning", 0)
            + parsed.get("code_volume", 0)
            + parsed.get("domain", 0)
            + parsed.get("context_length", 0)
        )
    except Exception as e:
        router._log(f"kimi scoring failed: {e}, score=0", "info")
        score = 0.0

    router._score_cache[fingerprint] = (score, now)
    router._log(f"score={score:.0f} for fingerprint={fingerprint[:8]}", "info")
    return score


async def async_pre_call_hook(
    self,
    user_api_key_dict,
    cache,
    data: dict,
    call_type,
) -> Optional[dict]:
    """路由入口：P0 OCR → P1 DeepSeek 关键词 → P2 Kimi 评分 → P3 兜底。

    LiteLLM 在每次请求前通过 litellm.callbacks 调用此方法，
    通过改写 data["model"] 实现动态模型分配。
    """
    prompt = _extract_user_prompt(data)
    if not prompt:
        self._log("no user prompt found, skipping routing", "debug")
        return data

    model = str(data.get("model", ""))
    self._log(f"routing call_type={call_type} model={model!r} prompt_len={len(prompt)}", "debug")

    # P0: OCR 关键词 → kimi-k2.6 (kimi 多模态能力更强)
    if _match_any_keyword(prompt, self.ocr_keywords):
        data["model"] = "kimi-k2.6"
        self._log("P0 OCR matched → kimi-k2.6", "info")
        return data

    # P1: DeepSeek 关键词 → deepseek-v4-pro (编程/架构任务)
    if _match_any_keyword(prompt, self.ds_keywords):
        data["model"] = "deepseek-v4-pro"
        self._log("P1 DeepSeek keyword matched → deepseek-v4-pro", "info")
        return data

    # P2: Kimi 四维评分 ≥ threshold → deepseek-v4-pro (复杂任务)
    score = await _kimi_score_prompt(prompt, self)
    if score >= self.score_threshold:
        data["model"] = "deepseek-v4-pro"
        self._log(f"P2 score={score:.0f} ≥ {self.score_threshold} → deepseek-v4-pro", "info")
        return data

    # P3: 兜底 → kimi-k2.6 (通用任务)
    data["model"] = "kimi-k2.6"
    self._log(f"P3 default → kimi-k2.6 (score={score:.0f})", "info")
    return data


# monkey-patch 到类上（与 __init__ 分离，保持文件结构清晰）
SmartRouter.async_pre_call_hook = async_pre_call_hook
