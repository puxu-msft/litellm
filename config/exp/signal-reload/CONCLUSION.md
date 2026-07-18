# PoC: signal 触发多文件包热重载

## 目标
多文件包无法靠单文件 mtime reload。验证「SIGUSR2 触发拓扑 reload」是否可行、
以及对进行中流式请求的影响。

## 结论(driver.py 实测)
1. ✅ SIGUSR2 handler 可注册,信号能到达 litellm 单进程,触发拓扑 reload
   (叶子 fixes → 依赖者 pipeline)后新代码生效。
2. ✅ 子模块修改正确刷新:改 fixes.py 后 reload,pipeline 用上新实现。
3. ⚠️ 进行中的生成器会被 reload 影响:未绑定旧模块引用的进行中流,reload 后
   后续 chunk 可能切到新实现(PoC 里 chunk3 从 v1 变 v2)。

## 采纳的设计(用户决策)
- **signal 只置标志,下次 hook 被调用(新请求)时才真正 reload**。
  - 进行中的流不受影响(其函数对象/闭包已绑定,且新请求才触发 reload)。
  - 贴合薄壳 async 无主循环结构:在 _get_impl() 里检查标志。
- handler 绝不在 signal 上下文里直接 reload(避免异步重入)。
- 触发方式: kill -USR2 $(pgrep -f "bin/python.*litellm"),或包一个 reload.sh。

## 拓扑 reload 顺序
按依赖逆序 reload:被依赖的叶子模块先 reload,再 reload 依赖它们的模块。
包设计时需声明或推导依赖顺序(见模块化设计)。
