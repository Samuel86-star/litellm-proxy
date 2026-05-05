"""
Codex Desktop → LiteLLM Proxy 的请求修补与工具过滤（单文件集中维护）。

为何存在本文件
    OpenAI Responses / Anthropic 才认识的 tool 类型（mcp、web_search 等）打到
    DeepSeek、火山方舟 Coding 等「仅 Chat 兼容」上游会 400；Codex 还会带
    OpenAI 新顶层字段、多模态块、V4 reasoning、超长 tools 等，需在进入上游前清洗。

与 run_proxy.py 的关系
    LiteLLM 的 litellm_settings.callbacks 不会触发 log_pre_api_call；
    run_proxy.py 在 run_server() 前把 DeepSeekV4ReasoningFix 注册进 input_callback，
    这样「即将 POST」的 complete_input_dict 才会被本文件里的 log_pre_api_call 处理。

文件内结构（自上而下）
    1. 代理路由：LITELLM_FORCE_PROXY_MODEL 覆盖 model。
    2. 消息工具：多模态折叠为纯文本、assistant/tool_calls 后补全 tool 消息链。
    3. 火山 Ark（Coding v3）：剥不兼容顶层字段、strict、schema 放松、再剥
       stream_options 等；VOLCANO_MAX_TOOLS / VOLCANO_TOOL_DESC_MAX_CHARS 限 tools。
    4. DeepSeekToolFilter：callbacks 里的 async_pre_call_hook — 非 Anthropic 时
       去掉非 function tools；火山路径下顺带 multimodal + ark_sanitize + tool_slim。
    5. DeepSeekV4ReasoningFix：input_callback + 可选 proxy 侧 pre_call — V4
       reasoning_content 补丁；DeepSeek 多模态与 tool 链；火山 multimodal、
       ark_sanitize、tool_slim、tool 链修复；log_pre_api_call 为 POST 前最后一刀。

环境变量速查
    LITELLM_FORCE_PROXY_MODEL   统一改写请求里的 model（LiteLLM 路由名）。
    VOLCANO_MAX_TOOLS           默认 12；0=不限制 function tools 数量。
    VOLCANO_TOOL_DESC_MAX_CHARS 默认 4000；0=不截断 function.description。

不必拆成多包：体量尚可、部署只需同目录 tool_filter.py；若再膨胀再考虑按
    volcano_*.py / deepseek_*.py 拆分。
"""

import os
from typing import Literal, Optional
from litellm.integrations.custom_logger import CustomLogger

# --- 1. 代理统一 model ------------------------------------------------------


def _apply_forced_proxy_model(data: Optional[dict], log_suffix: str = "") -> None:
    """若设置环境变量 LITELLM_FORCE_PROXY_MODEL，则覆盖请求里的 model（代理侧统一路由）。

    用于：Codex 主会话选了 Kimi，但 spawn_agent 等仍会传 gpt-5.4 等内置名时，
    仍强制走你在 LiteLLM 里配置的同一条 deployment。

    注意：子代理若名义上也变成 Kimi，实际能力仍是该后端模型，与名称无关。
    """
    fm = os.environ.get("LITELLM_FORCE_PROXY_MODEL", "").strip()
    if not fm or not isinstance(data, dict):
        return
    old = data.get("model")
    data["model"] = fm
    if old != fm:
        print(f"[force_proxy_model]{log_suffix} {old!r} -> {fm!r}")


def _normalize_msg_dict(m):
    if isinstance(m, dict):
        return m
    md = getattr(m, "model_dump", None)
    if callable(md):
        try:
            return md()
        except Exception:
            return None
    return None


def _tool_call_id_from_tool_message(m: dict) -> str:
    if not isinstance(m, dict):
        return ""
    for key in ("tool_call_id", "call_id"):
        v = m.get(key)
        if isinstance(v, str) and v.strip():
            return v
    return ""


# --- 2. 消息：多模态折叠、tool 链顺序 ----------------------------------------


def _normalize_messages_list(messages: list) -> None:
    """把 Pydantic 消息落成 dict，避免 .get('role')  silent fail。"""
    for idx in range(len(messages)):
        d = _normalize_msg_dict(messages[idx])
        if d is not None:
            messages[idx] = d


def _deepseek_flatten_single_message_content(msg: dict) -> bool:
    """DeepSeek Chat 请求体只接受 content 中的 text 类片段，不接受 OpenAI 的 image_url 等变体。

    Codex / 多模态客户端会传入 content=[{type:text},{type:image_url},...]，上游 Rust 反序列化直接 400。
    将列表折叠为单个字符串；图片等非文本块替换为简短占位说明。
    """
    c = msg.get("content")
    if isinstance(c, str):
        return False
    if not isinstance(c, list):
        return False

    text_parts: list[str] = []
    omitted_images = 0
    omitted_other = 0

    for p in c:
        if isinstance(p, str):
            text_parts.append(p)
            continue
        if not isinstance(p, dict):
            omitted_other += 1
            continue
        typ = p.get("type")
        if typ == "text":
            tx = p.get("text")
            if isinstance(tx, str):
                text_parts.append(tx)
            elif tx is not None:
                text_parts.append(str(tx))
        elif typ in (
            "image_url",
            "image",
            "input_image",
            "file",
            "input_file",
            "document",
        ):
            omitted_images += 1
        else:
            tx = p.get("text")
            if isinstance(tx, str) and tx.strip():
                text_parts.append(tx)
            else:
                omitted_other += 1

    out = "\n".join(x for x in text_parts if x)
    if omitted_images or omitted_other:
        note_parts = []
        if omitted_images:
            note_parts.append(f"{omitted_images} image(s)/multimodal part(s)")
        if omitted_other:
            note_parts.append(f"{omitted_other} other part(s)")
        note = (
            "\n[Note: " + ", ".join(note_parts)
            + " omitted — DeepSeek Chat API for this model expects text-only content.]"
        )
        out = (out + note).strip()

    if not out:
        out = "[No textual content: multimodal input was omitted for text-only DeepSeek endpoint.]"

    msg["content"] = out
    return True


def _deepseek_flatten_multimodal_in_messages(messages: list) -> int:
    if not isinstance(messages, list) or not messages:
        return 0
    _normalize_messages_list(messages)
    changed = 0
    for msg in messages:
        if isinstance(msg, dict) and _deepseek_flatten_single_message_content(msg):
            changed += 1
    return changed


def _repair_missing_tool_messages(messages: list) -> int:
    """DeepSeek：assistant + tool_calls 之后必须紧跟每条 tool_call_id 对应的 tool 消息。

    Codex / Responses 合并有时会把 tool 消息挪到 user 后面或散落列表中，仅「连续扫描」会漏判；
    这里对每个 assistant 工具轮：收集全局属于该轮 id 的 tool 消息，删掉旧位置，
    按 tool_calls 顺序紧插在 assistant 后面；仍缺的 id 用占位 tool（空 content）补齐。
    """
    if not isinstance(messages, list) or not messages:
        return 0

    _normalize_messages_list(messages)

    changed = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            i += 1
            continue

        raw_tcs = m.get("tool_calls")
        if not isinstance(raw_tcs, list) or not raw_tcs:
            i += 1
            continue

        required_order: list[str] = []
        id_to_name: dict[str, str] = {}
        for tc in raw_tcs:
            if hasattr(tc, "model_dump"):
                try:
                    tc = tc.model_dump()
                except Exception:
                    tc = None
            if not isinstance(tc, dict):
                continue
            tid = tc.get("id")
            if not isinstance(tid, str) or not tid:
                continue
            required_order.append(tid)
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                n = fn.get("name")
                if isinstance(n, str):
                    name = n
            id_to_name[tid] = name

        if not required_order:
            i += 1
            continue

        req_set = set(required_order)

        # 已连续紧跟 assistant 且 id 集齐 → DeepSeek 可接受（顺序按 tool_calls）
        ok_consecutive = True
        j = i + 1
        pos = 0
        while pos < len(required_order) and j < len(messages):
            tm = messages[j]
            if not isinstance(tm, dict) or tm.get("role") != "tool":
                ok_consecutive = False
                break
            tid = _tool_call_id_from_tool_message(tm)
            if tid != required_order[pos]:
                ok_consecutive = False
                break
            pos += 1
            j += 1
        if ok_consecutive and pos == len(required_order):
            # 所需 tool 已按序紧邻 assistant；若后面还有 tool，可能是重复/错位，仍走统一重排
            if j >= len(messages) or messages[j].get("role") != "tool":
                i = j
                continue

        # 收集 (索引, tid) 所有属于 req_set 的 tool 消息（整段列表里靠后的也算）
        recovered_first: dict[str, dict] = {}
        indices_rm: list[int] = []
        for k in range(i + 1, len(messages)):
            tm = messages[k]
            if not isinstance(tm, dict) or tm.get("role") != "tool":
                continue
            tid = _tool_call_id_from_tool_message(tm)
            if tid not in req_set:
                continue
            indices_rm.append(k)
            if tid not in recovered_first:
                recovered_first[tid] = tm

        indices_rm.sort(reverse=True)
        for k in indices_rm:
            messages.pop(k)

        block: list[dict] = []
        for tid in required_order:
            if tid in recovered_first:
                msg = recovered_first[tid]
                if not isinstance(msg, dict):
                    msg = _normalize_msg_dict(msg) or {}
                # 统一字段名，便于上游解析
                if _tool_call_id_from_tool_message(msg) != tid:
                    msg = {**msg, "role": "tool", "tool_call_id": tid}
                elif msg.get("tool_call_id") is None and msg.get("call_id"):
                    msg["tool_call_id"] = msg.get("call_id")
                block.append(msg)
            else:
                stub: dict = {
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": "",
                }
                nm = id_to_name.get(tid)
                if isinstance(nm, str) and nm:
                    stub["name"] = nm
                block.append(stub)
                changed += 1

        if indices_rm:
            changed += len(indices_rm)

        # 紧挨 assistant 插入有序 tool 块（单次切片，避免多次 insert 错位）
        messages[i + 1 : i + 1] = block

        i = i + 1 + len(block)

    return changed


# Upstreams that understand rich tool payloads natively and must NOT be filtered.
NATIVE_RICH_TOOL_PREFIXES = ("anthropic/", "claude-", "bedrock/anthropic", "vertex_ai/claude")

# --- 3. 火山 Ark：字段剥离、schema、tools 限额 ------------------------------


# 火山方舟 Coding（api/coding/v3）OpenAI 兼容层常拒绝 Codex / Responses 透传的 OpenAI 新参数字段。
_ARK_OPENAI_INCOMPAT_TOPLEVEL = frozenset(
    {
        "parallel_tool_calls",
        "service_tier",
        "store",
        "reasoning",
        "reasoning_effort",
        "prediction",
        "modalities",
        "verbosity",
        "audio",
        "safety_identifier",
        "web_search_options",
        "user",
        "top_logprobs",
        "logprobs",
        "stream_options",
        "thinking",
        "tool_resources",
        "prompt_cache_key",
        # Codex 通过 LiteLLM 传入，方舟 OpenAI 兼容层常不认识
        "extra_body",
        "metadata",
        "max_retries",
        "seed",
    }
)


def _is_volcano_coding_model(model: str) -> bool:
    m = (model or "").lower()
    if not m:
        return False
    needles = (
        "kimi",
        "glm-5",
        "glm-4",
        "minimax",
        "doubao",
        "ark-code",
        "ark-latest",
        "openai/glm",
        "openai/kimi",
        "openai/minimax",
        "openai/doubao",
        "openai/ark",
    )
    return any(n in m for n in needles)


def _strip_volcano_tool_strict_flags(body: dict) -> int:
    """部分方舟路由不接受 tools[].strict（OpenAI structured outputs 扩展）。"""
    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    n = 0
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.pop("strict", None) is not None:
            n += 1
        fn = t.get("function")
        if isinstance(fn, dict) and fn.pop("strict", None) is not None:
            n += 1
    return n


def _strip_volcano_ark_incompatible_params(body: dict) -> int:
    """就地删除/改写 body 顶层字段，返回大约变更次数（用于日志）。"""
    if not isinstance(body, dict):
        return 0
    n = 0
    for k in list(body.keys()):
        if k in _ARK_OPENAI_INCOMPAT_TOPLEVEL:
            body.pop(k, None)
            n += 1
    if "max_completion_tokens" in body:
        mct = body.pop("max_completion_tokens")
        n += 1
        if mct is not None and body.get("max_tokens") in (None, 0):
            body["max_tokens"] = mct
    n += _strip_volcano_tool_strict_flags(body)
    return n


_VOLCANO_LAST_CHANCE_KEYS = ("stream_options", "extra_body", "max_retries")


def _volcano_pop_reinjected_keys(body: dict) -> int:
    """LiteLLM 可能在 merge 阶段再次写入；POST 前强制拔掉。"""
    if not isinstance(body, dict):
        return 0
    n = 0
    for k in _VOLCANO_LAST_CHANCE_KEYS:
        if body.pop(k, None) is not None:
            n += 1
    return n


def _recursive_relax_json_schema_additional_properties(node) -> int:
    """删除 schema 中 additionalProperties: false（方舟对部分 Codex/OpenAI strict schema 校验过严）。"""
    n = 0
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            del node["additionalProperties"]
            n += 1
        for _k, v in list(node.items()):
            n += _recursive_relax_json_schema_additional_properties(v)
    elif isinstance(node, list):
        for item in node:
            n += _recursive_relax_json_schema_additional_properties(item)
    return n


def _volcano_relax_tool_function_schemas(body: dict) -> int:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return 0
    total = 0
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if isinstance(params, dict):
            total += _recursive_relax_json_schema_additional_properties(params)
    return total


def _apply_volcano_openai_compat_body(body: dict) -> dict[str, int]:
    """一次走完火山路径下的 body 清理，返回各步骤计数供日志使用。"""
    counts: dict[str, int] = {}
    if not isinstance(body, dict):
        return counts
    counts["strip"] = _strip_volcano_ark_incompatible_params(body)
    counts["relax_schema"] = _volcano_relax_tool_function_schemas(body)
    counts["last_pop"] = _volcano_pop_reinjected_keys(body)
    return counts


def _volcano_tool_limit_config() -> tuple[int | None, int | None]:
    """从环境变量读取火山路径 tools 限制。

    VOLCANO_MAX_TOOLS：最多保留的 function tools 数量。未设置时默认 12（缓解 Codex
    一次挂 15+ 工具时方舟 InvalidParameter）。设为 0 表示不限制。

    VOLCANO_TOOL_DESC_MAX_CHARS：每个工具的 function.description 最大字符数。
    未设置时默认 4000。设为 0 表示不截断。
    """
    mt_raw = os.environ.get("VOLCANO_MAX_TOOLS", "").strip()
    if mt_raw == "":
        max_tools = 12
    elif mt_raw == "0":
        max_tools = None
    else:
        try:
            max_tools = max(1, int(mt_raw))
        except ValueError:
            max_tools = 12

    dc_raw = os.environ.get("VOLCANO_TOOL_DESC_MAX_CHARS", "").strip()
    if dc_raw == "":
        max_desc = 4000
    elif dc_raw == "0":
        max_desc = None
    else:
        try:
            max_desc = max(256, int(dc_raw))
        except ValueError:
            max_desc = 4000

    return max_tools, max_desc


def _slim_volcano_tools_in_body(body: dict) -> dict[str, int]:
    """缩减火山请求中的 tools：数量上限 + 描述截断；就地修改 body。

    丢弃超出上限的工具时，将 tool_choice 置为 auto，避免指向已被移除的 tool。
    """
    stats: dict[str, int] = {"capped": 0, "desc_truncated": 0}
    if not isinstance(body, dict):
        return stats

    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return stats

    max_tools, max_desc = _volcano_tool_limit_config()

    kept: list = []
    for t in tools:
        if not isinstance(t, dict) or t.get("type") != "function":
            continue
        fn = t.get("function")
        if isinstance(fn, dict) and max_desc is not None:
            desc = fn.get("description")
            if isinstance(desc, str) and len(desc) > max_desc:
                fn["description"] = desc[:max_desc] + "\n…[truncated for volcano]"
                stats["desc_truncated"] += 1
        kept.append(t)

    if not kept:
        body.pop("tools", None)
        body.pop("tool_choice", None)
        return stats

    if max_tools is not None and len(kept) > max_tools:
        stats["capped"] = len(kept) - max_tools
        kept = kept[:max_tools]
        body["tool_choice"] = "auto"

    body["tools"] = kept
    return stats


# --- 4. LiteLLM callbacks：非 function tools 过滤 + 火山预处理 ----------------


class DeepSeekToolFilter(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "responses",
            "chat_completion",
        ],
    ) -> Optional[dict]:
        _apply_forced_proxy_model(data, f" {call_type}")
        model = str(data.get("model", "")).lower()

        if any(model.startswith(p) or f"/{p}" in model for p in NATIVE_RICH_TOOL_PREFIXES):
            return data

        if _is_volcano_coding_model(model):
            mf_vol = 0
            for key in ("messages", "input"):
                lst = data.get(key)
                if isinstance(lst, list):
                    mf_vol += _deepseek_flatten_multimodal_in_messages(lst)
            if mf_vol:
                print(
                    f"[volcano_multimodal_flatten] {call_type} model={model!r} "
                    f"flattened {mf_vol} message(s)"
                )
            vc = _apply_volcano_openai_compat_body(data)
            if any(vc.values()):
                print(
                    f"[volcano_ark_sanitize] {call_type} model={model!r} "
                    f"strip={vc.get('strip', 0)} relax_schema={vc.get('relax_schema', 0)} "
                    f"last_pop={vc.get('last_pop', 0)}"
                )

        tools = data.get("tools")
        if not isinstance(tools, list):
            return data

        original = len(tools)
        filtered = [
            t for t in tools
            if isinstance(t, dict) and t.get("type") == "function"
        ]
        dropped = original - len(filtered)

        if dropped > 0:
            print(
                f"[tool_filter] {call_type} model={model} "
                f"dropped {dropped}/{original} non-function tools "
                f"(kept {len(filtered)})"
            )

        if filtered:
            data["tools"] = filtered
        else:
            data.pop("tools", None)
            data.pop("tool_choice", None)

        if _is_volcano_coding_model(model) and isinstance(data.get("tools"), list):
            vs = _slim_volcano_tools_in_body(data)
            if any(vs.values()):
                print(
                    f"[volcano_tool_slim] {call_type} model={model} "
                    f"capped={vs.get('capped', 0)} "
                    f"desc_truncated={vs.get('desc_truncated', 0)}"
                )

        return data


deepseek_tool_filter = DeepSeekToolFilter()

# --- 5. DeepSeek V4：reasoning_content；input_callback 与 proxy 双路径 --------


def _deepseek_v4_patch_messages(messages: list) -> int:
    """Return number of assistant messages patched (in-place).

    DeepSeek V4 多轮：历史上每条 assistant 都必须带 `reasoning_content`（可为空串）。
    不能依赖「是否还能从消息里读到 reasoning」判断——LiteLLM 往往早已剥光。
    仅当列表里至少有一条 assistant 时才补丁，避免污染纯 user 单轮。
    """
    if not isinstance(messages, list):
        return 0
    if not any(
        isinstance(m, dict) and m.get("role") == "assistant" for m in messages
    ):
        return 0

    patched = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue

        rc = m.get("reasoning_content", None)
        if isinstance(rc, str):
            continue

        lifted = ""
        for block in m.get("thinking_blocks") or []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                text = block.get("thinking")
                if isinstance(text, str):
                    lifted = text
                    break

        m["reasoning_content"] = lifted
        patched += 1

    return patched


class DeepSeekV4ReasoningFix(CustomLogger):
    """
    Workaround for LiteLLM 1.83.x (BerriAI/litellm#26395):

    - Proxy `async_pre_call_hook` 执行太早，往往没有完整 messages，或 reasoning 已被剥。
    - 真正 POST 体在 `log_pre_api_call` 阶段的
      `kwargs["additional_args"]["complete_input_dict"]["messages"]`。

    因此必须 **register 到 litellm.input_callback**（模块加载时自动注册，run_proxy.py 会再调一次，由 manager 去重）；
    config 里保留 callbacks 项供 proxy 早期 async_pre_call_hook 路径使用。
    """

    def log_pre_api_call(self, model, messages, kwargs):  # noqa: ANN001
        # Logging.model 可能与 Router 展开后的真实模型不一致；必须以即将 POST 的 body 为准。
        md = kwargs if isinstance(kwargs, dict) else {}
        add = md.get("additional_args")
        body: dict = {}
        if isinstance(add, dict):
            cand = add.get("complete_input_dict")
            if isinstance(cand, dict):
                body = cand

        _apply_forced_proxy_model(body, " pre_api_call")

        body_model = str(body.get("model") or "").lower()
        meta_model = str(md.get("model") or "").lower()
        head_model = str(model or "").lower()
        combined = f"{head_model} {body_model} {meta_model}".lower()

        def _is_deepseek_v4_context() -> bool:
            if "deepseek" in combined and "v4" in combined:
                return True
            # thinking 打开时 DeepSeek 侧也会校验 reasoning_content
            th = body.get("thinking")
            if isinstance(th, dict) and th.get("type") == "enabled":
                if "deepseek" in body_model or "deepseek" in combined:
                    return True
            return False

        is_deepseek = "deepseek" in combined or "deepseek" in body_model

        reason_total = 0
        if _is_deepseek_v4_context():
            if body:
                api_msgs = body.get("messages")
                if isinstance(api_msgs, list):
                    reason_total += _deepseek_v4_patch_messages(api_msgs)

            if isinstance(messages, list):
                api_msgs = None
                if body:
                    api_msgs = body.get("messages")
                if messages is not api_msgs:
                    reason_total += _deepseek_v4_patch_messages(messages)

            if reason_total:
                print(
                    "[deepseek_v4_reasoning_fix] pre_api_call "
                    f"model_head={head_model!r} body_model={body_model!r} "
                    f"patched {reason_total} assistant message(s)"
                )

        if is_deepseek:
            # 多模态 image_url 等必须在最前面处理，否则 DeepSeek 反序列化直接 400
            flat_n = 0
            if body:
                api_msgs = body.get("messages")
                if isinstance(api_msgs, list):
                    flat_n += _deepseek_flatten_multimodal_in_messages(api_msgs)
            if isinstance(messages, list):
                api_msgs = body.get("messages") if body else None
                if messages is not api_msgs:
                    flat_n += _deepseek_flatten_multimodal_in_messages(messages)
            if flat_n:
                print(
                    "[deepseek_multimodal_flatten] pre_api_call "
                    f"model_head={head_model!r} body_model={body_model!r} "
                    f"flattened {flat_n} message(s) (image_url -> text)"
                )

            tool_fix = 0
            if body:
                api_msgs = body.get("messages")
                if isinstance(api_msgs, list):
                    tool_fix += _repair_missing_tool_messages(api_msgs)

            if isinstance(messages, list):
                api_msgs = body.get("messages") if body else None
                if messages is not api_msgs:
                    tool_fix += _repair_missing_tool_messages(messages)

            if tool_fix:
                print(
                    "[deepseek_tool_chain_fix] pre_api_call "
                    f"model_head={head_model!r} body_model={body_model!r} "
                    f"inserted {tool_fix} placeholder tool message(s)"
                )

        if _is_volcano_coding_model(body_model) or _is_volcano_coding_model(
            head_model
        ) or _is_volcano_coding_model(meta_model):
            if body:
                mf_vm = 0
                am = body.get("messages")
                if isinstance(am, list):
                    mf_vm += _deepseek_flatten_multimodal_in_messages(am)
                if mf_vm:
                    print(
                        "[volcano_multimodal_flatten] pre_api_call "
                        f"model_head={head_model!r} body_model={body_model!r} "
                        f"flattened {mf_vm} message(s)"
                    )
                vc = _apply_volcano_openai_compat_body(body)
                if any(vc.values()):
                    print(
                        "[volcano_ark_sanitize] pre_api_call "
                        f"model_head={head_model!r} body_model={body_model!r} "
                        f"strip={vc.get('strip', 0)} relax_schema={vc.get('relax_schema', 0)} "
                        f"last_pop={vc.get('last_pop', 0)}"
                    )

                vs = _slim_volcano_tools_in_body(body)
                if any(vs.values()):
                    print(
                        "[volcano_tool_slim] pre_api_call "
                        f"model_head={head_model!r} body_model={body_model!r} "
                        f"capped={vs.get('capped', 0)} "
                        f"desc_truncated={vs.get('desc_truncated', 0)}"
                    )

                tool_fix_v = 0
                if body:
                    am = body.get("messages")
                    if isinstance(am, list):
                        tool_fix_v += _repair_missing_tool_messages(am)
                if isinstance(messages, list):
                    am2 = body.get("messages") if body else None
                    if messages is not am2:
                        tool_fix_v += _repair_missing_tool_messages(messages)
                if tool_fix_v:
                    print(
                        "[volcano_tool_chain_fix] pre_api_call "
                        f"model_head={head_model!r} body_model={body_model!r} "
                        f"inserted {tool_fix_v} placeholder tool message(s)"
                    )

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "responses",
            "chat_completion",
        ],
    ) -> Optional[dict]:
        _apply_forced_proxy_model(data, f" {call_type} (reasoning_fix hook)")
        model = str(data.get("model", "")).lower()

        if _is_volcano_coding_model(model):
            mf_vol = 0
            for key in ("messages", "input"):
                lst = data.get(key)
                if isinstance(lst, list):
                    mf_vol += _deepseek_flatten_multimodal_in_messages(lst)
            if mf_vol:
                print(
                    f"[volcano_multimodal_flatten] {call_type} model={model!r} "
                    f"flattened {mf_vol} message(s) (reasoning_fix hook)"
                )
            vc = _apply_volcano_openai_compat_body(data)
            if any(vc.values()):
                print(
                    f"[volcano_ark_sanitize] {call_type} model={model!r} "
                    f"strip={vc.get('strip', 0)} relax_schema={vc.get('relax_schema', 0)} "
                    f"last_pop={vc.get('last_pop', 0)} (reasoning_fix hook)"
                )
            vs = _slim_volcano_tools_in_body(data)
            if any(vs.values()):
                print(
                    f"[volcano_tool_slim] {call_type} model={model!r} "
                    f"capped={vs.get('capped', 0)} "
                    f"desc_truncated={vs.get('desc_truncated', 0)} "
                    f"(reasoning_fix hook)"
                )
            tfv = 0
            _msgs = data.get("messages")
            if isinstance(_msgs, list):
                tfv += _repair_missing_tool_messages(_msgs)
            _inp = data.get("input")
            if isinstance(_inp, list):
                tfv += _repair_missing_tool_messages(_inp)
            if tfv:
                print(
                    f"[volcano_tool_chain_fix] {call_type} model={model} "
                    f"inserted {tfv} placeholder tool message(s) (reasoning_fix hook)"
                )

        if "deepseek" in model:
            flat = 0
            msgs = data.get("messages")
            if isinstance(msgs, list):
                flat += _deepseek_flatten_multimodal_in_messages(msgs)
            inp = data.get("input")
            if isinstance(inp, list):
                flat += _deepseek_flatten_multimodal_in_messages(inp)
            if flat:
                print(
                    f"[deepseek_multimodal_flatten] {call_type} model={model} "
                    f"flattened {flat} message(s) (proxy pre_call)"
                )

            tf = 0
            if isinstance(msgs, list):
                tf += _repair_missing_tool_messages(msgs)
            if isinstance(inp, list):
                tf += _repair_missing_tool_messages(inp)
            if tf:
                print(
                    f"[deepseek_tool_chain_fix] {call_type} model={model} "
                    f"inserted {tf} placeholder tool message(s) (proxy pre_call)"
                )

        if "deepseek" not in model or "v4" not in model:
            return data

        total = 0
        msgs = data.get("messages")
        if isinstance(msgs, list):
            total += _deepseek_v4_patch_messages(msgs)

        inp = data.get("input")
        if isinstance(inp, list):
            total += _deepseek_v4_patch_messages(inp)

        if total:
            print(
                f"[deepseek_v4_reasoning_fix] {call_type} model={model} "
                f"patched {total} assistant message(s) (proxy pre_call)"
            )

        return data


deepseek_v4_reasoning_fix = DeepSeekV4ReasoningFix()

# --- 6. 模块加载：确保 input_callback 已注册（无 run_proxy 时的兜底）--------


def _ensure_reasoning_fix_input_callback() -> None:
    """litellm_settings.callbacks 只会进入 litellm.callbacks，不会触发 log_pre_api_call。

    若未使用 run_proxy.py 注册 input_callback，则必须在模块加载时写入 litellm.input_callback，
    否则 DeepSeek V4 多轮 reasoning_content 修补永远不会执行。
    """
    import litellm

    litellm.logging_callback_manager.add_litellm_input_callback(deepseek_v4_reasoning_fix)


_ensure_reasoning_fix_input_callback()
