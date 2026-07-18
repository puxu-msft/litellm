# hookpkg 架构 / Package architecture

litellm hook 实现从单文件 `hook_impl.py`(1441 行)重构成多文件包 `hookpkg/`,并把
mtime 热重载换成 **SIGUSR2 触发的拓扑 reload**。本文档记录结构与关键机制。

## 目录结构(`~/.claude/litellm/`)

```
hooks.py                稳定薄壳:注册 SIGUSR2、sys.path 处理、委托 hookpkg。litellm 唯一直接加载的文件
reload.sh               kill -USR2 便捷触发
smoke_test.py           每个 config 分支跑最小请求,暴露被 try 吞掉的搬运缺陷
hookpkg/
  __init__.py           入口:process/process_deployment/observe_* + 请求侧辅助(_probe/_inject_tools/_fix_tool_choice/_strip_cache_control_scope/_fix_orphan_tool_use/_dump_*)
  reload.py             拓扑 reload + SIGUSR2 标志(RELOAD_ORDER 依赖逆序)
  config.py             配置热读(load_config,与 signal 无关,按 mtime 即时生效)
  probes.py             统一可观测:ProbeContext(audit/diag) + append_jsonl
  sse.py                sse_parse/sse_serialize/chunk_get/chunk_to_plain
  orphans.py            孤儿检测/修复(find_*/fix_openai_*),供 process_deployment/observe_*
  invoke_convert.py     泄漏 <invoke> 解析(extract_invoke_from_text)、白名单(name_in_whitelist,glob)、合成 tool_use/text 事件
  thinking.py           thinking block 请求侧修复(连续插空格、空 signature 转文本/删除、strip_all)
  stream.py             stream_transform 状态机(最长,调 fixes/sse/invoke_convert)
  fixes/
    __init__.py         apply_item_fixes 编排(意图优先级:string-to-array -> header-to-question)
    json_repair.py      json-fix(repair_truncated_json/loads_lenient,依赖 unicode_repair)
    unicode_repair.py   unicode-fix(repair_bad_unicode)
    coerce.py           string-to-array(coerce_items_type,内部复用 loads_lenient)
    fields.py           header-to-question(apply_field_fixes)
```

## SIGUSR2 拓扑热重载(取代 mtime)

多文件包无法靠单文件 mtime reload(reload 顶层模块不递归子模块)。改用:
- 薄壳导入时 `reload.install_signal_handler()` 注册 SIGUSR2 handler,收到信号**只置标志**
  (`_reload_pending=True`,绝不在 signal 上下文里 reload,避免异步重入)。
- 每次 hook 入口 `reload.maybe_reload()` 检查标志 -> 按 `RELOAD_ORDER`(依赖逆序:被依赖的
  叶子先 reload)拓扑 `importlib.reload` 整个包。
- **下一次请求才 reload** -> 进行中的流式请求不受影响(闭包已绑定旧模块)。
- reload 失败保留旧模块,绝不打断服务。

触发:`./reload.sh`(= `kill -USR2 $(pgrep -f "bin/python.*litellm")`)。

**关键区别**:
- 改 `hookpkg/` 代码 -> 发 SIGUSR2(reload.sh) -> 下次请求生效。
- 改 `hooks.config.json` -> 无需信号,config.py 按 mtime 即时热读。
- 改**薄壳 hooks.py 本身**(如新增 hook 方法) -> 必须**重启 litellm**(litellm 启动时只加载一次薄壳,`vars(cls)` 那时固定)。

PoC 见 `~/.claude/litellm/exp/signal-reload/CONCLUSION.md`。

## 统一可观测 ProbeContext(probes.py)

取代散落各处手写的 `if probe_only and probe_file: append_jsonl(...)`。

```python
ctx = ProbeContext.from_stream_fix(sf, model=..., call_id=...)
ctx.audit("patched", tool=name)        # 修复动作,落 audit_file,生产模式也记
ctx.diag("block_start", block_type=bt) # 诊断,仅 probe_only 落 probe_file
ctx.diag_enabled                       # 跳过昂贵的诊断构造
```

- **audit**:修复动作(patched/json_repaired/invoke_converted/parse_failed)。线上信号。
- **diag**:调试观测(chunk_shape/block_start/tool_input/text_leak_*/tool_use_seen/...)。仅 probe_only。
- 每事件自动带 `ts`(epoch)+`model`+`call_id`,类型字段名统一为 `event`(旧为 `_diag`)。
- 例外:deployment/failure/success 三个**独立探针子系统**各有自己的 file/enabled,不走
  ProbeContext(stream_fix 作用域),仍用 append_jsonl。

## 搬运缺陷的教训(sed 拆包)

单文件拆包时用 sed 批量改名(去下划线、加模块前缀),引入过**两个同源阻断 bug**,都被
薄壳的 try 吞掉导致**静默失效**:
1. `orphans` 局部变量遮蔽导入的 `orphans` 模块 -> `process_deployment` UnboundLocalError。
   修:导入别名 `_orphans`。
2. `_has_usable_tools`/`_tool_name` 漏搬(底层依赖没跟着走) -> 4 个请求侧函数 NameError。
   修:补回定义。

**防回归**:`smoke_test.py` 对每个 config 分支跑最小请求,把被 except 吞掉的 NameError/
UnboundLocalError 暴露成显式失败。这类搬运 bug 最危险的正是不报错。重构后必跑 smoke。

**通用排查**:用 AST 扫"函数内赋值遮蔽模块级导入名",可一次性揪出第 1 类冲突。
