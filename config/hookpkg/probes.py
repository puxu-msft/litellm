"""统一可观测 / Unified observability.

事件系统:修复/诊断逻辑通过 ProbeContext 声明"发生了什么",本模块统一决定是否落盘、
落到哪、格式如何。取代散落各处手写的 `if probe_only and probe_file: append_jsonl(...)`。

两类事件:
- **audit(审计)**:修复动作(patched/json_repaired/invoke_converted/parse_failed 等)。
  落 audit_file,**生产模式(probe_only 关)也记**——是"发生了什么修复"的线上信号。
- **diag(诊断)**:调试观测(chunk_shape/block_start/tool_input/... )。
  **仅 probe_only 时**落 probe_file——排错时抓真实数据用。

每个事件自动带元数据:ts(epoch 秒)、model、call_id。
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("litellm.hookpkg.probes")


def append_jsonl(path, rec):
    """把一条记录追加为 JSONL。失败静默(探针绝不影响主流程)。"""
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("hookpkg append_jsonl(%s) failed: %r", path, e)


class ProbeContext:
    """一次请求/一条流的可观测上下文。从 stream_fix 配置读落盘目标与开关。

    用法:
        ctx = ProbeContext.from_stream_fix(sf, model=..., call_id=...)
        ctx.audit("patched", tool=name)          # 修复动作,生产也记
        ctx.diag("block_start", block_type=bt)    # 诊断,仅 probe_only

    audit/diag 均在无对应文件配置或未开启时静默跳过,调用方无需自己判断。
    """

    def __init__(self, audit_file=None, probe_file=None, probe_only=False, model=None, call_id=None):
        self.audit_file = audit_file
        self.probe_file = probe_file
        self.probe_only = bool(probe_only)
        self.model = model
        self.call_id = call_id

    @classmethod
    def from_stream_fix(cls, sf, model=None, call_id=None):
        sf = sf or {}
        return cls(
            audit_file=sf.get("audit_file"),
            probe_file=sf.get("probe_file"),
            probe_only=sf.get("probe_only"),
            model=model,
            call_id=call_id,
        )

    def _meta(self, event_type, fields):
        rec = {"event": event_type}
        try:
            rec["ts"] = time.time()
        except Exception:
            pass
        if self.model is not None:
            rec["model"] = self.model
        if self.call_id is not None:
            rec["call_id"] = self.call_id
        rec.update(fields)
        return rec

    def audit(self, event_type, **fields):
        """记录一次修复动作。落 audit_file(生产模式也记)。无 audit_file 则跳过。"""
        if not self.audit_file:
            return
        append_jsonl(self.audit_file, self._meta(event_type, fields))

    def diag(self, event_type, **fields):
        """记录一次诊断观测。仅 probe_only 且有 probe_file 时落盘。"""
        if not self.probe_only or not self.probe_file:
            return
        append_jsonl(self.probe_file, self._meta(event_type, fields))

    @property
    def diag_enabled(self):
        """是否处于诊断模式(probe_only + 有 probe_file)。供调用方跳过昂贵的诊断构造。"""
        return self.probe_only and bool(self.probe_file)
