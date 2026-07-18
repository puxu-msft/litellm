# Round-tripping opaque provider state through a lossy protocol bridge (reasoning carrier)

问题类: 一个 provider（copilot Responses）有**不透明状态**（reasoning 的 `encrypted_content`），要跨一个**有损协议桥**（Anthropic Messages `/v1/messages`）往返到一个**不原生承载该状态的客户端**（Claude Code），使 provider 能跨轮拿回自己的状态。litellm 的 direct Responses adapter 原本在转换时把它丢了。

这类问题的通解 = **载体（carrier）机制 + 一套只能靠 live 验证的门禁方法论**。本文沉淀两者。实现: `litellm/llms/github_copilot/reasoning_carrier.py` 等；活文档 `docs/github_copilot_reasoning_bridge.md`。

## 一、载体模式（可复用）

把不透明状态序列化成**命名空间化 + 版本化**的 token，塞进客户端**会逐字节存储回放**的原生字段:

```
ghc-rsn:v1:<urlsafe_b64(json(payload))>
```

- **载体字段选择**: 找客户端会原样保存回放的「不透明元数据」字段。Anthropic 里是 `thinking.signature`（服务端不透明令牌，客户端必回放）或 `redacted_thinking.data`（加密推理块）——语义天然契合。**这是关键洞察: signature/data 就是为「客户端不可读、必须原样回放的服务端加密载荷」设计的**。
- **严格 decode 边界**（评审血泪，必做）: 用 Pydantic 校验 payload（拒非 str 元素、缺字段、空关键字段、多余键）；**严格 base64**（`validate=True`），否则插非法字符仍无损 decode、篡改检测形同虚设。返回 **tagged union**（`Decoded | NotOurs | Invalid | UnsupportedVersion`），**永不抛**——`NotOurs` 才进既有逻辑，损坏的安全丢弃 + 观测。
- **命名空间防碰撞**: 只认自己的 NS 前缀 + 版本；真客户端签名（无 NS）走 `NotOurs` 原样透传，绝不误解。
- **跨模型剥离**（安全，必做）: gpt-origin 载体若被回放进**别的 provider**（如 claude 后端会验签 → 400），必须在进入那条路径前剥离。零拷贝快路（无载体则原样返回，正常请求零影响）。
- **provider 门控**: 只对目标 provider 生效；其它 Responses provider（openai/azure）no-op。别让它悄悄改了别人的行为（实测: 不门控会把 `summary=auto` 注入 openai 请求、破坏其测试）。
- **kill switch**: 一个环境变量全局关（默认开），出事能秒退。

## 二、live 门禁方法论（本类问题的核心难点）

**R1 = 客户端是否原样存储并回放载体，是外部客户端行为，单元测试碰不到。** 这决定整个设计成立与否，必须 live 证。

### 用 subagent 驱动真实客户端

主会话是 opus，测不了 gpt 客户端行为。**派一个跑目标模型的 subagent**（如 `gpt-souls:general`，经同一代理走 gpt）——它本身就是一个真实 Claude Code 客户端，会像任何会话一样存储/回放回合。给它一个**多步推理任务**（逼出 reasoning + 多轮），跑完查它的 transcript。

- **路径陷阱**: subagent transcript 在 `~/.claude/projects/<proj>/<uuid>/subagents/agent-<id>.jsonl`——比 `projects/*/*.jsonl` **深一层**。`grep projects/*/*.jsonl` 会漏，得递归 `find`/`rglob`。曾因此误判「0 载体存活」。
- 判据: 解析 transcript 的 assistant thinking 块 `signature`，看是否 `<NS>:v1:` 起头且能 decode 出真实状态。实测: 551 回合子代理 146 个载体逐字节存活。

### 差分证明「重建真的到达后端」

光看「回放载体后 200」不够——可能后端只是**容忍**那个不透明块，并没用它。要证明代理真在**重建**并且后端在**验证**:

- **valid 载体回放 → 200**；**篡改内层 encrypted_content（结构仍合法）→ 400**。
- 若两者都 200 → 代理没在重建（载体被忽略）。**篡改必须触发后端拒绝**，才证明重建物真进了后端且被校验。这条差分是「reconstruction reaches backend」的唯一硬证。

### 严格 SDK vs 宽松客户端，分离「我方 bug」与「协议信封缺陷」

同一个响应流:
- **宽松客户端**（Claude Code）能容忍并正确显示；
- **严格 SDK**（`anthropic` python SDK 的 stream helper）可能丢内容。

实测: **双 `message_start`** 破坏严格 SDK 的 `thinking_delta` 累积（可见推理丢），但 Claude Code 正常。**别把「严格 SDK 丢了」当成自己的 bug**——它可能指向一个**协议信封缺陷**（多余/重复的信封事件）。两边都测才能定位。固化成 **xfail** 文档化，缺陷修好后 xpass 自动报警。

## 三、按需 e2e 测试落地

三组、各带自建 marker、默认 skip、不进 CI（`tests/e2e/github_copilot_reasoning/`，仿 `shutdown/` 套件的自建 marker 先例）:
- **SDK 组**: 真 `anthropic` SDK 打代理（非 `requests`，过 no-raw-requests 检查），验载体往返。
- **billed 组**（`LITELLM_RUN_BILLED=1`）: 真后端差分（valid/tamper）+ 连续性。
- **claude_cli 组**（`LITELLM_RUN_CLAUDE_CLI=1`）: subprocess 驱动真 `claude` CLI，验 R1 存储。

可自动化的往返（响应发→回放→请求重建、篡改不重建、backend-reach）用 mock/纯函数固化进 CI 单测；只有「真客户端存储」「真后端验证」这两件本质外部的事留按需 e2e。

## 相关

- 实现活文档: `docs/github_copilot_reasoning_bridge.md`（数据流落点、配置、偏离记录）
- spec/plan: `docs/superpowers/specs|plans/2026-07-14-gpt-reasoning-thinking-fidelity*`
- 门禁结果: `docs/superpowers/plans/2026-07-14-gpt-reasoning-poc-results.md`
- 方法论同源: `hot-reload-verification.md`（别拿间接推断否定可直接观测的机制，先找一手信号）
