"""session.py —— 复刻 dsh 的 packages/core/session

源码锚点（真源）：packages/core/session/src/types.ts + surface.ts

1. 14 种核心事件。
2. tool/call 的 arguments 是「未解析的原始 JSON 字符串」，不是 dict。
   dsh 刻意保留模型的原始输出，为的是回放时逐字节还原（types.ts 第 365-369 行）。
3. 投影（derive_messages）只有 5 类 surface 事件产生消息：
     user/message, system/message, developer/message, assistant/message, tool/result
   —— tool/call 本身【不】投影；工具调用是通过 assistant/message 里的
   tool_call content block 进历史的（surface.ts 第 120-152 行 deriveEventMessage）。
   —— assistant/attempt、turn/start、step/start 等「非 surface 事件」一律投影为 None。

对应 types.ts：
  SESSION_FORMAT_VERSION = 4
  TurnEndReason = completed | aborted | blocked | error | max-tokens | interrupted | forked
  SessionEvent = { type, seq, time, data }，surface 事件还带 surface_op / source_seq
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

## 14 种核心事件（SessionEventMap 的键）
SURFACE_EVENT_TYPES={
    "user/message",
    "system/message",
    "developer/message",
    "assistant/message",
    "tool/result",
}

# 会派生消息的只有上面这 5 类；下面这些只记日志、不投影
NON_SURFACE_EVENT_TYPES={
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "assistant/attempt",
    "tool/call",
    "request/header",
    "request/context",
    "session/end-seed",
}
ALL_EVENT_TYPES=SURFACE_EVENT_TYPES | NON_SURFACE_EVENT_TYPES

# 对应 TurnEndReasonMap —— 一轮为什么结束（7 种，merge-extensible）
TURN_END_REASONS={
    "completed","aborted","blocked","error",
    "max-tokens","interrupted","forked",
}