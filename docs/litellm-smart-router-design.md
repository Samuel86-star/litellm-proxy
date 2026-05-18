# LiteLLM 智能路由代理设计

> 状态：待审批
> 日期：2026-05-13
> 范围：在现有 LiteLLM 代理中新增请求级智能模型路由

## 1. 问题与目标

### 背景

当前 LiteLLM 配置将 Codex Desktop 所有别名（gpt-5.4 / gpt-5.5 等）静态映射到 kimi-k2.6。DeepSeek V4 Pro 虽然已配置但不会自动使用——需要手动 `cx deepseek-v4-pro` 切换。

### 目标

**请求级自动路由**：每条请求根据 prompt 特征动态分配最优模型，无需手动切换。

### 非目标

- 不替换 LiteLLM（仍在现有配置基础上扩展）
- 不修改 Codex Desktop 行为（路由对 Codex 完全透明）
- 不影响现有 tool_filter 清洗逻辑

---

## 2. 路由规则

### 优先级链（从高到低）

| 优先级 | 触发条件 | 目标模型 | 延迟 |
|:--:|------|------|:--:|
| P0 | prompt 含 OCR/图像关键词 | kimi-k2.6 | 0ms |
| P1 | prompt 含 DeepSeek 关键词 | deepseek-v4-pro | 0ms |
| P2 | Kimi 四维评分 ≥20 | deepseek-v4-pro | ~1-3s |
| P3 | 兜底 | kimi-k2.6 | — |

### P0 — OCR 硬路由（最高优先级）

**关键词：** `ocr` / `图片识别` / `图像识别` / `图文` / `截图` / `扫描` / `文字识别`

**原因：** DeepSeek V4 Pro 对 OCR 支持较弱，图片/截图类任务强制走 Kimi。

### P1 — DeepSeek 直通（中优先级）

**关键词：** `重构` / `设计` / `架构` / `optimize` / `debug` / `refactor` / `architect` / `写方案` / `提供方案`

**原因：** 复杂编码/推理任务直接走 DeepSeek，零延迟。

### P2 — Kimi 四维评分（低优先级）

未命中 P0/P1 时，调用 Kimi 对 prompt 按四维度打分：

| 维度 | 含义 | 分值 |
|------|------|:--:|
| reasoning | 推理深度：逻辑推断、多步推理、抽象思考 | 1-5 |
| code_volume | 代码量：预期生成的代码行数/文件数 | 1-5 |
| domain | 领域知识：特定框架/系统/算法的深度知识 | 1-5 |
| context_length | 上下文长度：需要理解大量已有代码/文档 | 1-5 |

**阈值：≥20（即 4×5 满分）才转 DeepSeek。**

**评分 Prompt：**

```
你是一个任务复杂度评估器。对以下任务按四个维度打分（各 1-5 分）：
1. 推理深度：逻辑推断、多步推理、抽象思考的强度
2. 代码量：预期生成的代码行数/文件数
3. 领域知识：是否需要特定框架、系统、算法的深度知识
4. 上下文长度：是否需要理解大量已有代码/文档
只返回 JSON：{"reasoning":N,"code_volume":N,"domain":N,"context_length":N}
```

---

## 3. 架构

### 数据流

```
Codex Desktop
  │  POST /v1/chat/completions  { model: "gpt-5.2", messages: [...] }
  ▼
LiteLLM Proxy (:4000)
  │
  │  model mapping: gpt-5.2 → kimi-k2.6
  │
  │  callbacks pipeline:
  │    ├── deepseek_tool_filter         (现有，不变)
  │    ├── deepseek_v4_reasoning_fix    (现有，不变)
  │    ├── router_hook                  (新增)
  │    │     │
  │    │     ├─ OCR 关键词？       → model = "kimi-k2.6"      (直接返回)
  │    │     ├─ DeepSeek 关键词？  → model = "deepseek-v4-pro" (直接返回)
  │    │     └─ 其他               → Kimi 评分 → 决定 model
  │    │
  │    ▼
  │  LiteLLM 重新路由到最终 model
  │
  ▼
目标后端 API (DeepSeek / Volcano Ark)
```

### 部署

`router_hook.py` 放在 LiteLLM 运行目录（`~/litellm/`，即 `run_proxy.py` 同级），`config.yaml` 中 `router_hook.router_hook` 的 import 路径相对于该目录。

### 组件

| 组件 | 文件 | 类型 | 职责 |
|------|------|------|------|
| RouterHook | `router_hook.py`（新建） | `CustomLogger` | 路由决策核心，实现 `async_pre_call_hook` |
| 关键词匹配器 | `router_hook.py` | 纯函数 | 正则匹配 P0/P1 关键词 |
| 复杂度评分器 | `router_hook.py` | 异步方法 | 调用 Kimi 评分，JSON 解析 |
| 评分缓存 | 内存 `dict` | 内建 | prompt 指纹 → 评分结果，TTL 5min |
| 配置 | 环境变量 | — | 关键词列表、阈值、TTL 等 |
| 注册点 | `config.yaml` | `callbacks` | 挂载到 LiteLLM 回调链 |

### 与现有工具的关系

```
litellm_settings.callbacks:
  - tool_filter.deepseek_tool_filter          # P1: 工具清洗（不变）
  - tool_filter.deepseek_v4_reasoning_fix     # P2: reasoning 补丁（不变）
  - router_hook.router_hook                   # P3: 智能路由（新增）
```

互不干扰：路由钩子只改 `data["model"]`，不在 tools/messages 上操作。

---

## 4. 关键设计决策

### 4.1 融入 LiteLLM（而非独立代理）

**选择原因：**
- 零新进程，不动 launchd plist
- 复用现有 LiteLLM 的 callback 机制
- 工具过滤和路由在同一个请求管道，避免重复解析

**代价：**
- 强耦合到 LiteLLM 版本 API
- 路由钩子与工具过滤共享 LiteLLM 进程生命周期

### 4.2 关键词 + LLM 评分组合（而非纯规则或纯 LLM）

**选择原因：**
- 关键词层：零延迟，覆盖高频/明确任务（OCR→Kimi、重构→DeepSeek）
- LLM 评分层：覆盖灰色地带，用模型判断代替人工规则
- 保守阈值（≥20）：宁可多走 Kimi，也不把复杂任务错分给 DeepSeek

**舍弃方案：**
- 纯规则：边界 case 多，规则膨胀快
- 纯 LLM：每次请求多一次 API 调用，延迟不可接受
- 级联（先用 DeepSeek，不行再 Kimi）：延迟翻倍，用户体验差

### 4.3 保守阈值（≥20 分才切 DeepSeek）

**约束：** 4 维度各最高 5 分，总分 20 为满分。

**选择 ≥20（非 ≥14 或 ≥10）：** 只有 Kimi 自评「我搞不定」时才让给 DeepSeek。这对应「宁可多花钱，别给错答案」原则。B 类误判（复杂任务错分给 DeepSeek）的实际代价远大于 A 类。

### 4.4 评分调用 Kimi 而非 DeepSeek

**原因：**
- 自我评估：让 Kimi 判断任务是否超出自己能力
- 保持一致性：路由层和兜底层为同一服务商，降低延迟抖动

### 4.5 Codex SQLite model 字段无关性

Codex 在 `state_5.sqlite` 中记住了 model 名（如 `gpt-5.2`），但路由发生在 LiteLLM 收到请求之后，改写的是 LiteLLM 内部路由目标。Codex 的记忆完全不影响最终发到哪个后端。唯一影响是 UI 中的模型名显示——这是 cosmetic 问题。

---

## 5. 容错策略

| 场景 | 行为 | 原因 |
|------|------|------|
| Kimi 评分 API 超时（>10s） | 走 kimi-k2.6 | 不因为评分延迟阻塞用户 |
| Kimi 评分返回非 JSON | 走 kimi-k2.6 | 容错，不尝试解畸形响应 |
| 评分请求网络错误 | 走 kimi-k2.6 | 降级到安全默认 |
| 请求中无 user message | 走 kimi-k2.6 | 无法评分，安全兜底 |
| 所有 user message 为空 | 走 kimi-k2.6 | 无法提取有意义的 prompt |
| 相同 prompt（指纹）5min 内重复 | 走缓存结果 | 避免重复评分开销 |

**日志：** 每次路由决策记录：`[token_cost, decision, reason]`。debug 模式打印评分详情 JSON。

---

## 6. 配置

所有配置通过环境变量注入，无配置文件。

```bash
# 关键词路由
ROUTER_OCR_KEYWORDS="ocr,图片识别,图像识别,图文,截图,扫描,文字识别"
ROUTER_DEEPSEEK_KEYWORDS="重构,设计,架构,optimize,debug,refactor,architect,写方案,提供方案"

# 评分
ROUTER_SCORE_THRESHOLD=20        # 4×5=20，满分才转 DeepSeek
ROUTER_SCORE_CACHE_TTL=300       # 评分结果缓存 5 分钟

# 日志级别
ROUTER_LOG_LEVEL=info            # info|debug；debug 打印评分 JSON

# 评分 Prompt
ROUTER_SCORING_SYSTEM_PROMPT=none         # 可选覆盖；默认使用硬编码评分 Prompt
ROUTER_SCORING_TEMPERATURE=0.0   # 确定性输出，确保评分稳定
ROUTER_SCORING_TIMEOUT=10        # Kimi 评分 API 超时秒数
```

> 评分 Prompt 硬编码在 `router_hook.py` 中，`ROUTER_SCORING_SYSTEM_PROMPT` 环境变量仅用于自定义覆盖，一般不设置。
ROUTER_SCORING_TEMPERATURE=0.0   # 确定性输出，确保评分稳定
ROUTER_SCORING_TIMEOUT=10        # Kimi 评分 API 超时秒数
```

---

## 7. 实现范围

### 文件变更

| 文件 | 操作 | 说明 |
|------|:--:|------|
| `router_hook.py` | 新建 | 路由钩子主逻辑 |
| `config.yaml` | 修改 | 注册 `router_hook.router_hook` 到 callbacks |
| `run_proxy.py` | 不改 | router_hook 走 `litellm_settings.callbacks`，不涉及 `input_callback` |
| `tool_filter.py` | 不改 | 路由逻辑完全独立，不影响工具清洗 |

### 不在范围内

- 不改 Codex Desktop 行为
- 不改 LiteLLM 路由/重试/超时配置
- 不新增进程、端口、launchd 服务
- 不做请求级日志持久化（打印到 stderr 即可）
- 不做评分结果持久化（内存缓存重启即清）

---

## 8. 验证标准

### 功能验证

- [ ] OCR 关键词命中 → 请求发到 kimi-k2.6
- [ ] DeepSeek 关键词命中 → 请求发到 deepseek-v4-pro
- [ ] 未命中关键词 → Kimi 评分调用正常
- [ ] 评分 ≥20 → 走 deepseek-v4-pro
- [ ] 评分 <20 → 走 kimi-k2.6
- [ ] 评分调用超时 → 走 kimi-k2.6
- [ ] 相同 prompt 重复 → 命中缓存，不多次评分
- [ ] 评分结果不符合 JSON → 走 kimi-k2.6
- [ ] 不打断现有 tool_filter 清洗

### 性能验证

- [ ] 关键词路由不引入可感知延迟（<5ms）
- [ ] 评分路由延迟 <3s（含 Kimi API 调用）
- [ ] 评分缓存命中延迟 <1ms

---

## 9. 已知局限

| 局限 | 影响 | 缓解 |
|------|------|------|
| 评分基于单条请求，不理解跨轮对话上下文 | 可能在多轮对话中评分偏低 | 保守阈值缓解：大部分情况仍走 Kimi |
| 缓存使用 prompt 指纹，不含系统/历史消息 | 相同 prompt 不同上下文可能错用缓存 | TTL 短（5min）缓解 |
| 路由决策完全在代理侧，Codex 无感知 | UI 模型名与实际后端可能不一致 | cosmetic 问题，不影响功能 |
| LiteLLM 版本升级可能破坏 callback API | 路由钩子需适配 | RouterHook 是独立小模块，易于修复 |

---

## 10. 附录：关键词路由完整列表

### OCR 关键词（→ kimi-k2.6）

```
ocr
图片识别
图像识别
图文
截图
扫描
文字识别
```

### DeepSeek 关键词（→ deepseek-v4-pro）

```
重构
设计
架构
optimize
debug
refactor
architect
写方案
提供方案
```
