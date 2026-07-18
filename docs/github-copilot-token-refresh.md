# GitHub Copilot API key 提前刷新（proactive refresh）

> 状态：已实现，在 `ghc` 分支本地（私有 fork,按约定不进上游）。落点 `litellm/llms/github_copilot/authenticator.py`,配套后台循环间隔在 `litellm/llms/github_copilot/model_capabilities.py`。
> 关联：后台刷新循环与端点缓存见 [端点路由设计](./github-copilot-endpoint-routing.md)。

## 问题

用户高频看到日志刷屏：

```
authenticator.py:95 - API key expired, refreshing
```

每约 30 分钟必现一次,且刷新发生在请求关键路径上（`get_api_key` 被每次 chat/messages/responses/embedding 转换同步调用),那一刻的请求要等一次同步 HTTP 刷新才能继续。

## 根因（why）

Copilot 的 api-key 生命周期约 30 分钟(实测 `expires_at - 获取时刻 ≈ 1801s`),token 响应里带 `refresh_in`(实测 1500s = 25 分钟),这是 GitHub 明确给出的「发放后多少秒就该刷新」信号,官方 Copilot 客户端正是据此在过期前约 5 分钟提前刷新。

原 `get_api_key` 的判定是纯懒惰刷新：`expires_at > now` 就直接返回缓存 key,否则才刷新并打 WARNING。问题在于它**只在真过期后才刷新**,完全没用上 `refresh_in`。更浪费的是:proxy 启动时本就有一个每隔一段时间调 `get_api_key` 的后台循环(见端点路由文档),它本有大把机会在过期前刷掉 key,却因判定条件太严(必须真过期)每次拿到未过期的 key 就原样返回。于是只能等 token 真过期那一刻,某个请求或某轮后台循环撞上,才刷新 + 刷屏。

## 设计（how）

判定逻辑抽成纯函数 `_should_refresh_api_key(now, expires_at, refresh_in, obtained_at)`,对齐官方语义：

1. **有 `refresh_in` 且知道获取时刻** → `now >= obtained_at + refresh_in` 即到刷新点就刷新（过期前约 5 分钟)。这是主路径
2. **回退**：缺 `refresh_in` 或获取时刻不可知 → 退回原「`now >= expires_at` 才刷新」的懒惰行为,不破坏老 token 文件

「获取时刻」`obtained_at` 的来源分两层：
- 刷新写盘时把 `last_refreshed`(当前时间戳)一并写入 `api-key.json`,下次读取直接用,不依赖对 TTL 的猜测
- 外部工具(如官方 gh cli)写的 token 没有 `last_refreshed` 字段时,回退用文件 mtime 作为获取时刻的代理,让这类 token 也能享受提前刷新

读取路径 `_read_cached_api_key` 用 Pydantic `_CachedAPIKey` 把 `json.load` 的 `Any` 收敛成 typed 字段(容忍 `endpoints`/`annotations` 等额外字段),再喂给纯函数判定。原来那段绕圈的 `raise APIKeyExpiredError` + `except pass` 控制流一并改成清晰的早返回;正常提前刷新的日志从 WARNING 降到 DEBUG(提前刷新是常态,不该刷屏)。

## 双保险：后台循环 + 请求路径

- **后台循环**：`periodic_capability_refresh_loop` 的间隔从 300s 降到 120s。因为刷新窗口(进入刷新点到真过期)正好约 5 分钟,而原 300s = 5 分钟恰好等于窗口宽度、无任何余量(最坏情况后台在刚过刷新点前一刻检查、下次要等满 5 分钟,任何抖动就错过退回真过期刷新)。120s 让 5 分钟窗口内稳稳命中 2~3 次
- **请求路径兜底**：每个请求都会调 `get_api_key`,过了刷新点的第一个请求也会触发提前刷新。即便后台循环某轮错过,只要窗口内有流量就不会撞真过期

两者叠加,正常运行下 key 永远在过期前被换掉,用户既看不到 WARNING,也不会有请求卡在过期边界的刷新延迟上。

## 代码地图

- `litellm/llms/github_copilot/authenticator.py`：`_CachedAPIKey`(Pydantic 视图)、`_should_refresh_api_key`(纯判定函数)、`Authenticator._read_cached_api_key` / `_api_key_file_mtime`、`get_api_key`(改为早返回 + 写 `last_refreshed`)
- `litellm/llms/github_copilot/model_capabilities.py`：`_REFRESH_INTERVAL_SECONDS` 300 → 120

## 测试地图

- `tests/test_litellm/llms/github_copilot/test_github_copilot_authenticator.py`：
  - `TestShouldRefreshAPIKey`：纯函数在刷新点/前一秒/无 `refresh_in`/无 `obtained_at` 各边界(强杀 `>=`、`+`、None 分支变异)
  - `test_get_api_key_refreshes_at_refresh_in_before_expiry`：**核心回归**——token 未过期但已过 `refresh_in` 点必须刷新(旧代码返回旧 key、会失败)
  - `test_get_api_key_not_refreshed_before_refresh_in`：窗口内不过度刷新
  - `test_get_api_key_uses_file_mtime_when_last_refreshed_missing`：外部 token 用 mtime 兜底
  - `test_get_api_key_persists_last_refreshed_on_refresh`：刷新写盘保留 `last_refreshed` 与上游字段
</content>
</invoke>
