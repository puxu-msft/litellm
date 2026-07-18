"""模拟薄壳:注册 SIGUSR2 -> 拓扑 reload 整个包。验证子模块修改能否生效、
以及进行中的生成器是否受影响。"""
import signal, sys, os, time, importlib, threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkg import pipeline, fixes

_reload_flag = threading.Event()

def _on_signal(signum, frame):
    _reload_flag.set()  # signal handler 只置标志,真正 reload 在主循环做(避免在 handler 里重入)

signal.signal(signal.SIGUSR2, _on_signal)

def _reload_pkg():
    # 拓扑顺序:先 reload 叶子(fixes),再 reload 依赖它的(pipeline)
    global pipeline, fixes
    importlib.reload(fixes)
    importlib.reload(pipeline)
    print(f"[reloaded] fixes.VERSION={fixes.VERSION}", flush=True)

print(f"PID={os.getpid()} ready. initial: {pipeline.run('hello')}", flush=True)

# 启动一个"进行中的生成器"模拟流式请求,看 reload 是否打断它
def slow_gen():
    fn = pipeline.run  # 绑定当前函数对象(模拟进行中的请求已绑定旧实现)
    for i in range(6):
        yield fn(f"chunk{i}")
        time.sleep(0.5)

gen = slow_gen()
print("gen start:", next(gen), flush=True)

# 主循环:等信号 -> reload -> 继续消费生成器
for i in range(10):
    if _reload_flag.is_set():
        _reload_flag.clear()
        _reload_pkg()
        print("after reload, pipeline.run('new'):", pipeline.run('new'), flush=True)
    try:
        print("gen next:", next(gen), flush=True)
    except StopIteration:
        pass
    time.sleep(0.5)
print("done", flush=True)
