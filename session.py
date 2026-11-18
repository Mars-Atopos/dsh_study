"""session.py —— 复刻 dsh 的 packages/core/session

源码锚点（真源）：packages/core/session/src/types.ts + surface.ts

这一讲要复刻的「真相」：

1.  14 种核心事件。
   
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

# 14 种核心事件（SessionEventMap 的键）
SURFACE_EVENT_TYPES = {
    "user/message",
    "system/message",
    "developer/message",
    "assistant/message",
    "tool/result",
}
# 会派生消息的只有上面这 5 类；下面这些只记日志、不投影
NON_SURFACE_EVENT_TYPES = {
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
ALL_EVENT_TYPES = SURFACE_EVENT_TYPES | NON_SURFACE_EVENT_TYPES

# 对应 TurnEndReasonMap —— 一轮为什么结束（7 种，merge-extensible）
TURN_END_REASONS = {
    "completed", "aborted", "blocked", "error",
    "max-tokens", "interrupted", "forked",
}


class Session:
    """append-only 事件日志：一次对话的唯一真相（source of truth）。

    关键不变量（对应 types.ts 顶部注释）：
    - append-only：事件只追加，不修改、不删除。
    - 每个事件有单调递增的 seq 和毫秒时间戳 time。
    - 消息历史是从日志「投影」出来的，不是独立存储的。
    """

    def __init__(self, path: Path | None = None):
        self.events: list[dict] = []
        self.path = path
        self._seq = 0  # 对应 SessionSeq，从 0 起（源码里 seq 单调递增）

    # ── 写入 ──────────────────────────────────────────────
    def append(self, event_type: str, data: dict, surface_op: str = "append") -> dict:
        """追加一条事件。surface 事件带 surface_op（append / replace）。

        返回这条事件本身，方便调用方拿到 seq（源码里 append 会返回 seq，
        例如 assistant/message 落地后回填引用）。
        """
        if event_type not in ALL_EVENT_TYPES:
            raise KeyError(f"未知事件类型: {event_type!r}（不是核心 14 种之一）")
        event = {
            "type": event_type,
            "seq": self._seq,
            "time": int(time.time() * 1000),
            "data": data,
        }
        if event_type in SURFACE_EVENT_TYPES:
            event["surface_op"] = surface_op
        self._seq += 1
        self.events.append(event)
        self._flush(event)
        return event

    def _flush(self, event: dict) -> None:
        """写一行 JSONL（lossless JSON，对应 types.ts 的「lossless JSON」）。"""
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── 读取 / 恢复 ───────────────────────────────────────
    @classmethod
    def load(cls, path: Path) -> "Session":
        """从 JSONL 恢复（模拟进程重启后重新加载）。"""
        s = cls(path=path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            s.events.append(event)
            s._seq = event["seq"] + 1  # 恢复 seq 游标
        return s

    # ── 投影：从日志导出模型可见历史 ──────────────────────
    def derive_messages(self) -> list[dict]:
        """从事件流确定性折叠出「模型看到的历史」（OpenAI 兼容格式）。

        对应 surface.ts 的 deriveEventMessage + foldSurface：
        - 只有 5 类 surface 事件派生消息
        - assistant/message 里的 tool_call block 会转成 tool_calls 字段
        - assistant/attempt、tool/call、turn/step 边界等一律跳过
        - 空 content 的 system/developer/assistant 派生为 None（跳过）
        """
        out: list[dict] = []
        for e in self.events:
            t = e["type"]
            data = e.get("data", {})

            if t == "user/message":
                out.append({"role": "user", "content": data["content"]})

            elif t == "system/message":
                if data.get("content"):
                    out.append({"role": "system", "content": data["content"]})

            elif t == "developer/message":
                # dsh 里 developer 消息默认空 content 不派生；非空才投影
                msg = data.get("message", {})
                if msg.get("content"):
                    out.append({"role": "developer", "content": msg["content"]})

            elif t == "assistant/message":
                # content 是 block 列表：[{"type":"text",...}, {"type":"tool_call",...}]
                blocks = data.get("content", [])
                text = "".join(
                    b["text"] for b in blocks if b.get("type") == "text"
                )
                tool_calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {"name": b["name"], "arguments": b["arguments"]},
                    }
                    for b in blocks if b.get("type") == "tool_call"
                ]
                # 空 assistant（既无文本也无工具调用）不派生 —— 对应源码「empty assistant hosts max-tokens usage」
                if not text and not tool_calls:
                    continue
                msg: dict = {"role": "assistant", "content": text or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)

            elif t == "tool/result":
                # tool/call 本身不投影；它的结果作为 role=tool 消息进入历史
                out.append({
                    "role": "tool",
                    "tool_call_id": data["call_id"],
                    "content": data["content"],
                })

            # 其余：assistant/attempt、tool/call、turn/start、request/header … 全部跳过
        return out


# ── 自测 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile

    s = Session()
    s.append("turn/start", {"turn": 1})
    s.append("user/message", {"content": "看下这个仓库"})
    # 1) attempt 不进历史（失败/重试凭据）
    s.append("assistant/attempt", {"turn": 1, "step": 1, "stream": ["失败重试"]})
    # 2) assistant/message 里带一个 tool_call block —— 这才是「模型想调工具」的落点
    s.append("assistant/message", {"content": [
        {"type": "tool_call", "id": "c1", "name": "greet", "arguments": '{"name":"Ada"}'},
    ]})
    # 3) tool/call 这条日志事件本身不投影
    s.append("tool/call", {"callId": "c1", "name": "greet", "arguments": '{"name":"Ada"}'})
    # 4) tool/result 才作为 role=tool 进历史
    s.append("tool/result", {"call_id": "c1", "content": "Hello, Ada!"})
    s.append("step/end", {"turn": 1, "step": 1})
    s.append("turn/end", {"turn": 1, "reason": "completed"})

    msgs = s.derive_messages()
    print("投影出的消息数:", len(msgs))
    for m in msgs:
        print(" ", m)

    # 断言：attempt / tool/call / turn 边界都不进历史，恰好 3 条
    assert len(msgs) == 3, f"期望 3 条，实际 {len(msgs)}"
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["tool_calls"][0]["function"]["name"] == "greet"
    assert msgs[2]["role"] == "tool" and msgs[2]["content"] == "Hello, Ada!"
    print("✅ 通过：attempt/tool-call/边界不投影，tool_call 经 assistant 进历史")

    # 5) 进程重启后重建：逐字段相等
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "s.jsonl"
        s2 = Session(path=p)
        s2.append("user/message", {"content": "hi"})
        s2.append("assistant/message", {"content": [{"type": "text", "text": "yo"}]})
        restored = Session.load(p)
        assert restored.derive_messages() == s2.derive_messages()
        print("✅ 通过：重启后从 JSONL 重建的历史与写入前逐字段相等")
