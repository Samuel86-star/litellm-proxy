# LiteLLM 智能路由代理 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 LiteLLM 代理中新增 `router_hook.py`，根据 prompt 关键词+Kimi 评分动态分配 deepseek-v4-pro/kimi-k2.6

**Architecture:** 单一新文件 `router_hook.py` 作为 LiteLLM CustomLogger，通过 `async_pre_call_hook` 改写 `data["model"]`。P0 OCR 关键词直接路由→kimi，P1 DeepSeek 关键词直通，P2 调用 Kimi 四维评分按阈值决策。内存缓存评分结果 TTL 5min。

**Tech Stack:** Python 3, LiteLLM proxy (CustomLogger + async_pre_call_hook), aiohttp, re/hashlib

---

### Task 1: 环境变量配置

**Files:**
- Modify: `~/litellm/.env`（追加路由相关环境变量）

- [ ] **Step 1: 追加路由环境变量到 .env**

```bash
cat >> ~/litellm/.env << 'ENVEOF'

# --- Smart Router ---
ROUTER_OCR_KEYWORDS="ocr,图片识别,图像识别,图文,截图,扫描,文字识别"
ROUTER_DEEPSEEK_KEYWORDS="重构,设计,架构,optimize,debug,refactor,architect,写方案,提供方案"
ROUTER_SCORE_THRESHOLD=20
ROUTER_SCORE_CACHE_TTL=300
ROUTER_LOG_LEVEL=info
ROUTER_SCORING_TEMPERATURE=0.0
ROUTER_SCORING_TIMEOUT=10
ENVEOF
echo "Appended router env vars to ~/litellm/.env"
```

- [ ] **Step 2: 验证环境变量写入正确**

```bash
grep "^ROUTER_" ~/litellm/.env
```

Expected: 7 行以 `ROUTER_` 开头的环境变量。

---

### Task 2: 创建 router_hook.py（基础结构与配置加载）

**Files:**
- Create: `~/litellm/router_hook.py`

- [ ] **Step 1: 创建文件骨架并写配置加载逻辑**

```python
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
```

- [ ] **Step 2: 验证 Python 语法无错误**

```bash
cd ~/litellm && python3 -c "import router_hook; print('SmartRouter loaded OK')"
```

Expected: `SmartRouter loaded OK`

---

### Task 3: 实现关键词匹配器

**Files:**
- Modify: `~/litellm/router_hook.py`（追加 _extract_user_text、_match_keywords 方法）

- [ ] **Step 1: 追加 user message 提取与关键词匹配方法**

在 `SmartRouter` 类中 `__init__` 方法之后追加：

```python
    def _extract_user_text(self, data: dict) -> str:
        """从请求 data 中提取最近一条 user message 的文本内容."""
        messages = data.get("messages", [])
        if not messages:
            return ""
        # 取最后一条 user message
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # 多模态 content 数组：只取 text 部分
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                    return " ".join(parts)
                return str(content)
        return ""

    def _match_keywords(self, text: str, keywords: list[str]) -> bool:
        """检查 text 中是否包含任意关键词（大小写不敏感）."""
        text_lower = text.lower()
        for kw in keywords:
            # 英文关键词：单词边界匹配
            if kw.isascii() and not any("\u4e00" <= c <= "\u9fff" for c in kw):
                if re.search(r"\b" + re.escape(kw) + r"\b", text_lower):
                    return True
            # 中文关键词：子串匹配
            elif kw.lower() in text_lower:
                return True
        return False
```

- [ ] **Step 2: 验证方法可导入且不报错**

```bash
cd ~/litellm && python3 -c "
from router_hook import router_hook
d = {'messages': [{'role': 'user', 'content': '帮我看下截图里有什么文字'}]}
t = router_hook._extract_user_text(d)
print(repr(t))
print('match ocr:', router_hook._match_keywords(t, ['ocr', '截图']))
print('match ds:', router_hook._match_keywords(t, ['重构', '架构']))
"
```

Expected:
```
'帮我看下截图里有什么文字'
match ocr: True
match ds: False
```

---

### Task 4: 实现 Kimi 评分调用

**Files:**
- Modify: `~/litellm/router_hook.py`（追加 _score_via_kimi、_parse_score 方法）

- [ ] **Step 1: 追加评分调用和 JSON 解析方法**

在 `SmartRouter` 类中追加：

```python
    async def _score_via_kimi(self, user_text: str, api_key: str) -> Optional[int]:
        """调用 Kimi K2.6 对用户文本打分，返回总分或 None（调用失败）."""
        import aiohttp

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "kimi-k2.6",
            "messages": [
                {"role": "system", "content": self.scoring_system_prompt},
                {"role": "user", "content": user_text[:2000]},
            ],
            "temperature": self.scoring_temperature,
            "max_tokens": 200,
        }

        self._log(f"calling Kimi scorer for text[:80]={user_text[:80]!r}", "debug")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=self.scoring_timeout),
                ) as resp:
                    if resp.status != 200:
                        self._log(f"Kimi scorer HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    return self._parse_score(content)
        except Exception as e:
            self._log(f"Kimi scorer exception: {e}")
            return None

    def _parse_score(self, raw: str) -> Optional[int]:
        """从 Kimi 返回的 JSON 中提取四维总分. 格式异常返回 None."""
        raw = raw.strip()
        # 尝试提取第一个 JSON object（处理可能有 markdown 包裹的情况）
        import re as _re
        m = _re.search(r"\{[^{}]*\}", raw)
        if not m:
            self._log(f"scorer response not JSON: {raw[:200]}")
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            self._log(f"scorer JSON decode failed: {raw[:200]}")
            return None
        return (
            obj.get("reasoning", 0)
            + obj.get("code_volume", 0)
            + obj.get("domain", 0)
            + obj.get("context_length", 0)
        )
```

- [ ] **Step 2: 验证 import 依赖**

```bash
cd ~/litellm && python3 -c "import aiohttp; print('aiohttp OK')"
```

如果 `aiohttp` 未安装：

```bash
pip3 install aiohttp
```

---

### Task 5: 实现评分缓存与 prompt 指纹

**Files:**
- Modify: `~/litellm/router_hook.py`（追加 _make_fingerprint、_get_cached_score 方法）

- [ ] **Step 1: 追加缓存相关方法**

在 `SmartRouter` 类中追加：

```python
    def _make_fingerprint(self, user_text: str) -> str:
        """对 user_text 做确定性指纹."""
        return hashlib.md5(user_text.encode("utf-8")).hexdigest()

    def _get_cached_score(self, fingerprint: str) -> Optional[int]:
        """检查缓存中是否有未过期的评分."""
        if fingerprint not in self._score_cache:
            return None
        ts, score = self._score_cache[fingerprint]
        if time.time() - ts > self.cache_ttl:
            del self._score_cache[fingerprint]
            return None
        return score

    def _set_cached_score(self, fingerprint: str, total_score: int) -> None:
        """设置评分缓存."""
        self._score_cache[fingerprint] = (time.time(), total_score)
```

- [ ] **Step 2: 验证缓存逻辑**

```bash
cd ~/litellm && python3 -c "
from router_hook import router_hook
fp = router_hook._make_fingerprint('hello')
print('fingerprint:', fp)
router_hook._set_cached_score(fp, 15)
print('cached:', router_hook._get_cached_score(fp))
print('miss:', router_hook._get_cached_score('nonexistent'))
print('cache OK')
"
```

Expected:
```
fingerprint: 5d41402abc4b2a76b9719d911017c592
cached: 15
miss: None
cache OK
```

---

### Task 6: 实现核心路由钩子 async_pre_call_hook

**Files:**
- Modify: `~/litellm/router_hook.py`（追加 async_pre_call_hook 方法）

- [ ] **Step 1: 追加路由决策主方法**

在 `SmartRouter` 类中追加：

```python
    async def async_pre_call_hook(self, data: dict, **kwargs) -> Optional[dict]:
        """LiteLLM 回调：在路由前改写 model 字段."""
        user_text = self._extract_user_text(data)
        if not user_text:
            self._log("no user text, fallthrough to kimi")
            return data

        current_model = data.get("model", "unknown")
        final_model = "kimi-k2.6"
        reason = "default"

        # P0: OCR 关键词 → kimi-k2.6（显式确认）
        if self._match_keywords(user_text, self.ocr_keywords):
            final_model = "kimi-k2.6"
            reason = "ocr_keyword"
            self._log(f"P0 OCR → {final_model} | {user_text[:60]}")

        # P1: DeepSeek 关键词 → deepseek-v4-pro
        elif self._match_keywords(user_text, self.ds_keywords):
            final_model = "deepseek-v4-pro"
            reason = "deepseek_keyword"
            self._log(f"P1 DS-keyword → {final_model} | {user_text[:60]}")

        # P2: Kimi 评分
        else:
            fp = self._make_fingerprint(user_text)
            cached = self._get_cached_score(fp)
            if cached is not None:
                total_score = cached
                self._log(f"P2 score(cached)={total_score} | {user_text[:60]}", "debug")
            else:
                api_key = os.environ.get("LITELLM_MASTER_KEY", "")
                if not api_key:
                    self._log("no LITELLM_MASTER_KEY for scoring, fallthrough")
                    total_score = 0
                else:
                    total_score = await self._score_via_kimi(user_text, api_key) or 0
                    if total_score > 0:
                        self._set_cached_score(fp, total_score)
                    self._log(f"P2 score(live)={total_score} | {user_text[:60]}", "debug")

            if total_score >= self.score_threshold:
                final_model = "deepseek-v4-pro"
                reason = f"score_{total_score}"
            else:
                final_model = "kimi-k2.6"
                reason = f"score_{total_score}_too_low"

        self._log(f"route: {current_model} → {final_model} ({reason})")
        data["model"] = final_model
        return data
```

- [ ] **Step 2: 验证路由钩子导入无语法错误**

```bash
cd ~/litellm && python3 -c "
import router_hook
h = router_hook.router_hook
print('async_pre_call_hook:', hasattr(h, 'async_pre_call_hook'))
print('RouterHook loaded OK')
"
```

Expected:
```
async_pre_call_hook: True
RouterHook loaded OK
```

---

### Task 7: 注册路由钩子到 LiteLLM config.yaml

**Files:**
- Modify: `~/litellm/config.yaml`

- [ ] **Step 1: 在 `litellm_settings.callbacks` 中追加 router_hook**

找到 `config.yaml` 中的 `litellm_settings:` 区域，在 `callbacks:` 列表末尾追加 `router_hook.router_hook`。

修改后的 callbacks 应为：

```yaml
litellm_settings:
  drop_params: true
  set_verbose: false
  telemetry: false
  request_timeout: 600
  num_retries: 2
  callbacks:
    - tool_filter.deepseek_tool_filter
    - tool_filter.deepseek_v4_reasoning_fix
    - router_hook.router_hook
```

**修改方式**：用 sed 在最后一个 ` - tool_filter.` callback 行后插入：

```bash
cd ~/litellm
# 在 deepseek_v4_reasoning_fix 行后插入 router_hook.router_hook
sed -i '' '/deepseek_v4_reasoning_fix/a\
    - router_hook.router_hook
' config.yaml
```

- [ ] **Step 2: 验证 config.yaml 修改正确**

```bash
grep -A1 "router_hook" ~/litellm/config.yaml
```

Expected: 显示包含 `router_hook.router_hook` 的行。

---

### Task 8: 端到端验证（重启 LiteLLM + 烟雾测试）

**Files:**
- 无新建文件（验证阶段）

- [ ] **Step 1: 重启 LiteLLM 服务**

```bash
~/Documents/litellm-proxy/litellmctl.sh reload
sleep 5
~/Documents/litellm-proxy/litellmctl.sh status
```

Expected: LiteLLM 状态正常，端口 4000 在监听。

- [ ] **Step 2: 测试 OCR 关键词路由**

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "帮我识别下这个截图里的文字"}],
    "max_tokens": 50
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('model:', d.get('model','?'))"
```

Expected: 响应正常，`model` 为 `kimi-k2.6`（实际走后端为 kimi）。

- [ ] **Step 3: 测试 DeepSeek 关键词路由**

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "帮我重构一下这个模块的设计"}],
    "max_tokens": 50
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('model:', d.get('model','?'))"
```

Expected: 响应正常，`model` 为 `deepseek-v4-pro`（实际走后端为 DeepSeek）。

- [ ] **Step 4: 查看路由日志**

```bash
tail -20 ~/Library/Logs/LiteLLM/stderr.log | grep "SmartRouter"
```

Expected: 看到 `P0 OCR → kimi-k2.6` 和 `P1 DS-keyword → deepseek-v4-pro` 路由记录。

- [ ] **Step 5: 测试普通消息走 Kimi 评分**

```bash
curl -sS http://127.0.0.1:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{
    "model": "gpt-5.2",
    "messages": [{"role": "user", "content": "Python 里 args 和 kwargs 有什么区别"}],
    "max_tokens": 50
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('model:', d.get('model','?'))"
```

Expected: 响应正常，路由日志显示评分结果（通常 <20 走 kimi）。

---

### Task 9: Git 提交

**Files:**
- 无新建文件

- [ ] **Step 1: 提交所有变更**

```bash
cd ~/litellm
git add router_hook.py config.yaml .env
git commit -m "feat(router): add smart model router with keyword + Kimi scoring"
```
