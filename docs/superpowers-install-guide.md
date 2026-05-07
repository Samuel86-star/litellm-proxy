# Superpowers 安装指南（Claude Code / Codex / Cursor）

> **适用场景**：在新机器上为三大 AI 编程助手统一安装 Superpowers 工作流插件  
> **版本**：v5.1.0 (superpowers-dev)  
> **预计时间**：10-15 分钟

---

## 一、前置检查

打开终端，依次执行以下命令确认环境：

```bash
# 1. 检查 Node.js 和 npm（Claude Code 插件需要）
node --version   # 要求 >= 18
npm --version

# 2. 检查各 CLI 是否已安装
claude --version
codex --version
# Cursor 无 CLI，确认已安装 Cursor.app

# 3. 确认主目录结构
ls -la ~/.claude ~/.codex ~/.cursor 2>/dev/null
```

如缺少某项，请先安装对应工具再继续。

---

## 二、Claude Code 安装（✅ 原生支持插件）

### 步骤 1：添加 Marketplace 源
```bash
claude plugin marketplace add superpowers-dev https://github.com/obra/superpowers.git
```

### 步骤 2：安装插件
```bash
claude plugin install superpowers@superpowers-dev
```

### 步骤 3：验证安装
```bash
claude plugin list
# 期望输出包含：superpowers@superpowers-dev (enabled)
```

### 步骤 4：（可选）卸载旧插件
如果之前装过 `claude-hud` 或其他冲突插件：
```bash
claude plugin uninstall claude-hud
```

---

## 三、Codex 安装（⚠️ 无插件系统，使用 AGENTS.md 注入）

Codex 没有 CLI 插件管理机制，需手动将工作流规范写入全局 `AGENTS.md`。

### 步骤 1：创建全局配置目录（如不存在）
```bash
mkdir -p ~/.codex
```

### 步骤 2：创建 RTK 规则文件
创建 `~/.codex/RTK.md`，内容如下：

```markdown
# RTK - Rust Token Killer (Codex CLI)

**Usage**: Token-optimized CLI proxy for shell commands.

## Rule

Always prefix shell commands with `rtk`.

Examples:

```bash
rtk git status
rtk cargo test
rtk npm run build
rtk pytest -q
```

## Meta Commands

```bash
rtk gain            # Token savings analytics
rtk gain --history  # Recent command savings history
rtk proxy <cmd>     # Run raw command without filtering
```

## Verification

```bash
rtk --version
rtk gain
which rtk
```
```

### 步骤 3：创建全局 AGENTS.md
创建 `~/.codex/AGENTS.md`，内容如下：

```markdown
# AGENTS.md — Superpowers 工作流规范（Codex 适配版）

> 本文件改编自 [obra/superpowers](https://github.com/obra/superpowers)。
> 在 Codex CLI 中使用：这些指令覆盖默认系统行为，但**用户直接指令始终优先**。

## 铁律（不可违反）

| 编号 | 规则 | 违反后果 |
|------|------|----------|
| 1 | **无测试不写代码** — 先写失败测试，再写最少实现 | 删除已写代码，从头开始 |
| 2 | **无根因不修复** — 调试必须先找到根因，才能提修复方案 | 返回到 Phase 1 重新排查 |
| 3 | **无验证不声明完成** — 必须运行验证命令并确认输出 | 撤销完成声明，重新验证 |
| 4 | **无设计不实现** — 任何创造性工作必须先通过设计评审 | 停止实现，回到设计阶段 |
| 5 | **技能优先** — 如果某项工作有对应技能（哪怕只有 1% 可能适用），必须先查阅技能内容 | 重新从技能步骤开始 |

## 核心工作流触发条件

### 1. Brainstorming（需求/设计阶段）
**触发条件：** 用户提出任何"创建功能"、"构建组件"、"添加行为"、"修改功能"的请求。

**必须完成的步骤：**
1. 探索项目上下文（文件结构、文档、近期 commit）
2. 如果需要视觉反馈，提供浏览器预览选项
3. 逐条提问澄清需求（一次一个问题，优先多选）
4. 提出 2-3 种方案，说明优劣和推荐
5. 分节呈现设计，每节获得用户批准后再继续
6. 撰写设计文档保存到 `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
7. 自审：扫描 TBD/TODO/矛盾/歧义/范围过大
8. 用户评审书面 spec，获批后再进入实现阶段
9. **唯一出口：** 调用 writing-plans 技能创建实现计划

**硬门控：** 未经用户明确批准设计，不得写任何代码、不得创建项目脚手架。

---

### 2. Writing Plans（计划阶段）
**触发条件：** 已有经过批准的设计 spec，需要创建实现计划。

**关键原则：**
- 假设实现者对代码库零上下文、品味可疑
- 提供完整路径、完整代码、完整命令、预期输出
- 每个步骤 = 一个动作（2-5 分钟）
- DRY、YAGNI、TDD、频繁提交

**计划文档结构：**
```markdown
# [Feature] Implementation Plan

> **For agentic workers:** 使用子 Agent 逐任务执行或批量执行。

## 目标
[1-2 句话描述本计划要实现什么]

## 前提条件
- [ ] 设计 spec 已批准：[链接]
- [ ] 相关 issue/PR：[链接]

## 任务列表

### 任务 1：[名称]
**预计时间：** X 分钟
**详细步骤：**
1. 具体命令或代码修改
2. 预期输出
3. 验证方式

### 任务 2：[名称]
...

## 验证清单
- [ ] 所有测试通过
- [ ] 功能符合设计 spec
- [ ] 文档已更新

## 回滚计划
如果失败，如何回滚到之前状态：
```

**硬门控：** 未经用户明确批准计划，不得开始实现。

---

### 3. TDD（测试驱动开发）
**触发条件：** 任何代码实现阶段。

**严格执行红-绿-重构循环：**

1. **红：** 先写一个失败的测试
   - 测试名必须描述期望行为
   - 测试失败信息必须清晰
   - 运行测试确认它确实失败

2. **绿：** 写最少代码让测试通过
   - 允许硬编码、复制粘贴、丑陋代码
   - 目标：测试通过，不考虑优雅

3. **重构：** 在不改变行为的前提下改进代码
   - 检查：是否有重复？命名是否清晰？是否过度设计？
   - 每次重构后运行全部测试

**硬门控：** 没有失败测试，不允许写实现代码。

---

### 4. Systematic Debugging（系统化调试）
**触发条件：** 遇到 bug、测试失败、意外行为。

**严禁：** 猜原因 → 改代码 → 看好了没（Stab-in-the-dark）

**必须遵循的 5 个阶段：**

#### Phase 1: 复现
- 写出最小复现步骤
- 确认 100% 可复现
- 记录环境信息（版本、配置、依赖）

#### Phase 2: 观察
- 添加日志/断点收集数据
- 记录实际行为 vs 期望行为
- 不要试图修复，只收集信息

#### Phase 3: 假设
- 基于观察提出 2-3 个可能原因
- 为每个假设设计验证实验
- 按概率排序

#### Phase 4: 验证
- 对每个假设执行验证实验
- 记录实验结果
- 排除不可能的原因

#### Phase 5: 修复
- 只在找到根因后才允许修改代码
- 修改必须针对根因，不是症状
- 修复后必须添加回归测试

**硬门控：** 没有找到根因，不允许提修复方案。

---

### 5. Executing Plans（执行计划）
**触发条件：** 已有批准的实施计划，需要执行。

**执行原则：**
- 严格按步骤执行，不跳过、不提前
- 每完成一个任务，勾选并简要总结
- 遇到意外情况（步骤不适用、发现新风险），立即暂停并报告用户
- 不要擅自修改计划，除非用户明确同意

**子 Agent 使用规范：**
- 复杂任务使用 `spawn_agent` 分配给子 Agent
- 每个子 Agent 必须有明确、有限的职责
- 主 Agent 负责协调和验证结果

---

### 6. Finishing Branch（分支收尾）
**触发条件：** 功能开发完成，需要合并或提交。

**必须完成的检查清单：**
- [ ] 所有测试通过（包括新测试）
- [ ] 代码自审：检查 obvious mistakes
- [ ] 文档更新（README、API 文档、CHANGELOG）
- [ ] 提交信息符合规范（conventional commits）
- [ ] 无调试代码、无 console.log、无临时文件
- [ ] 如果涉及 UI，提供截图或预览

---

### 7. Verification Gate（验证门控）
**触发条件：** 用户说"完成"、"好了"、"试试"等表示任务结束的话。

**必须执行：**
1. 重新阅读原始需求
2. 对照需求验证实际输出
3. 运行相关测试确认通过
4. 如发现问题，如实报告，不隐瞒
5. 只有在验证通过后，才确认任务完成

**硬门控：** 未经验证，不允许声明任务完成。

---

### 8. Parallel Agents（并行 Agent）
**触发条件：** 任务包含多个独立子任务。

**执行方式：**
- 识别可并行化的子任务
- 为每个子任务 spawn 独立 Agent
- 明确每个 Agent 的职责边界
- 收集所有结果后再整合
- 适用于：多文件重构、批量数据处理、多模块测试

---

## 工具映射（Superpowers → Codex）

| Superpowers 概念 | Codex 等价工具 | 用法 |
|------------------|----------------|------|
| `plan` | `update_plan` | 创建/更新执行计划 |
| `spawn` | `spawn_agent` | 创建子 Agent 并行工作 |
| `review` | `codex review` | 代码评审（CLI 命令） |
| `test` | `pytest`/`cargo test`/etc | 运行测试框架 |
| `debug` | `exec_command` + 日志分析 | 系统化调试 |
| `verify` | 运行验证命令 + 人工确认 | 验证门控 |

---

## 备注

- 本文件位于 `~/.codex/AGENTS.md`，对**所有项目生效**
- 如需对特定项目覆盖，可在项目根目录创建 `AGENTS.md`
- 用户直接指令始终优先于本规范
- 不确定时，询问用户而非猜测
```

### 步骤 4：验证加载
启动新的 Codex 会话，输入测试指令：
```bash
codex
```

然后问 AI："帮我写一个加法函数"，观察 AI 是否：
1. 先询问需求细节（Brainstorming）
2. 要求先写测试（TDD 铁律）
3. 调用 `update_plan` 创建计划

如遵循上述流程，说明 AGENTS.md 加载成功。

---

## 四、Cursor 安装（⚠️ 无 CLI 插件系统，需手动配置）

Cursor 目前**不支持 CLI 安装插件**，需要手动复制文件并在 UI 中启用。

### 步骤 1：获取插件源码

**方式 A：从 GitHub 克隆（推荐）**
```bash
mkdir -p ~/.cursor/plugins/local
git clone https://github.com/obra/superpowers.git ~/.cursor/plugins/local/superpowers-dev
```

**方式 B：从其他机器复制**
如果另一台机器已安装 Claude Code 插件，直接复制：
```bash
# 在已安装机器上
scp -r ~/.claude/plugins/marketplaces/superpowers-dev user@新机器:~/.cursor/plugins/local/
```

### 步骤 2：在 Cursor UI 中启用
1. 打开 Cursor App
2. 点击左上角 `Cursor` → `Settings`（或按 `Cmd/Ctrl + ,`）
3. 选择左侧 `Plugins` 标签
4. 点击 `Install from folder...` 或 `Add local plugin`
5. 选择 `~/.cursor/plugins/local/superpowers-dev` 目录
6. 启用插件，重启 Cursor

### 步骤 3：验证
新建一个对话，询问 AI "帮我写一个功能"，观察是否遵循 Superpowers 工作流（先设计、再计划、后实现）。

---

## 五、快速验证清单

全部安装完成后，逐一验证：

| 工具 | 验证命令/操作 | 期望结果 |
|------|--------------|----------|
| Claude Code | `claude plugin list` | 显示 `superpowers@superpowers-dev` |
| Codex | 新会话问 "写个功能" | AI 先要求写测试 |
| Cursor | Settings → Plugins | 显示 superpowers-dev 已启用 |

---

## 六、常见问题

### Q1: Claude Code 安装提示 "Plugin not found in marketplace"
先更新 marketplace 索引：
```bash
claude plugin marketplace update superpowers-dev
```
然后重试安装。

### Q2: Codex 的 AGENTS.md 没有生效
- 确认文件路径是 `~/.codex/AGENTS.md`（不是 `.codex/agents.md`）
- 启动新会话（已运行的会话不会重新加载）
- 检查是否有项目级 `AGENTS.md` 覆盖了全局配置

### Q3: Cursor 找不到插件文件夹按钮
Cursor 的插件系统正在演进，如 UI 中没有选项：
- 确保 Cursor 版本 >= 0.40
- 或尝试将规则写入 Cursor 的 `.cursorrules` 文件作为替代：
  ```bash
  echo "[粘贴 AGENTS.md 的核心规则]" > ~/.cursorrules
  ```

### Q4: 如何卸载？
```bash
# Claude Code
claude plugin uninstall superpowers

# Codex
rm ~/.codex/AGENTS.md ~/.codex/RTK.md

# Cursor
在 Settings → Plugins 中禁用并删除
```

---

## 七、文件路径速查

| 文件 | 路径 |
|------|------|
| Claude Code 插件目录 | `~/.claude/plugins/marketplaces/superpowers-dev/` |
| Claude Code 已安装列表 | `~/.claude/installed_plugins.json` |
| Codex 全局 AGENTS.md | `~/.codex/AGENTS.md` |
| Codex RTK 规则 | `~/.codex/RTK.md` |
| Codex 配置文件 | `~/.codex/config.toml` |
| Cursor 本地插件 | `~/.cursor/plugins/local/superpowers-dev/` |
| Cursor 全局规则 | `~/.cursorrules` |

---

## 八、参考链接

- Superpowers 源码：https://github.com/obra/superpowers
- Claude Code 插件文档：https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/plugins
- Codex AGENTS.md 规范：https://github.com/openai/codex/tree/main#agentsmd

---

*文档生成时间：2026-05-07*  
*适配版本：superpowers-dev v5.1.0*
