"""tools.py —— 复刻 dsh 的 packages/core/tools

源码锚点（真源）：packages/core/tools/src/schema.ts + events.ts + pipeline.ts

这一讲要复刻的「真相」：

1. defineTool 的四个职责分离（schema.ts 第 484-546 行 DefineToolOptions）：
     description  -> 模型选不选你
     parameters  -> 参数对不对（schema 单一来源：一份 Pydantic 既喂模型又校验）
     output.schema -> 返回值长什么样（canonical 值）
     output.render -> 模型读不读得懂（转成 ContentBlock）
2. 校验在【execute 内部第一行】做，不是在 execute 之前（schema.ts 第 598-602 行：
     defineTool 包装的 execute 先 validate(args) 再调 userExecute）。
     所以「模型自我修正」的触发点，是 execute 抛出的 ToolArgsError。
3. 三段钩子是 waterfall（events.ts）：
     tools/pre-execute  -> allow/deny/ask/cancel，next() 委托 allow
     tools/execute       -> around-dispatch（超时/重试/埋点）
     tools/post-execute  -> accept/replace/block
   —— 而 tools/result 是 emit（observe-only），改它不影响任何东西。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

# ── 决策三态（对应 PreToolDecision）───────────────────────
DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_ASK = "ask"
DECISION_CANCEL = "cancel"


class ToolArgsError(ValueError):
    """对应源码的 ToolArgsError（schema.ts 第 462-471 行）。

    模型生成的参数不合法时抛出，violations 是逐条违规说明。
    这正是「模型自我修正」的原料：模型看到错误后按 schema 改。
    """

    def __init__(self, violations: list[str]):
        super().__init__("invalid arguments: " + "; ".join(violations))
        self.violations = violations


@dataclass
class Tool:
    """define_tool 的产物 —— 对应 schema.ts 里 defineTool 返回的 ToolDefinition。"""
    name: str
    description: str
    args_model: type[BaseModel]          # parameters 的单一来源
    execute_fn: Callable[..., Any]       # 真正的业务逻辑
    output_render: Callable[[Any], Any] | None = None  # 把返回值转成模型可见内容

    def json_schema(self) -> dict:
        """喂给模型的参数 schema（对应 projectToolSchema）。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }

    def validate(self, raw_args: Any) -> BaseModel:
        """校验参数。非法则抛 ToolArgsError（对应 validateArgs）。"""
        try:
            return self.args_model.model_validate(raw_args)
        except ValidationError as e:
            raise ToolArgsError([f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]) from e


# 钩子类型：pre 返回决策；post 返回（替换后的）结果
PreHook = Callable[[Tool, Any], str | Awaitable[str]]
PostHook = Callable[[Tool, Any, Any], Any | Awaitable[Any]]


@dataclass
class ToolRegistry:
    """工具注册表 —— 对应 ctx.tools（core/tools 的 ToolsService）。

    三段流水线都在这里调度：pre-execute -> execute -> post-execute。
    tools/result 是 observe-only，用 result_hooks 表示（改它不影响执行）。
    """
    tools: dict[str, Tool] = field(default_factory=dict)
    pre_hooks: list[PreHook] = field(default_factory=list)
    post_hooks: list[PostHook] = field(default_factory=list)
    result_hooks: list[Callable[..., None]] = field(default_factory=list)

    def register(self, tool: Tool) -> Tool:
        self.tools[tool.name] = tool
        return tool

    def tool_schemas(self) -> list[dict]:
        return [t.json_schema() for t in self.tools.values()]

    async def execute(self, name: str, raw_args: Any) -> dict:
        """走完整流水线跑一个工具。返回 {ok, value|reason}。"""
        tool = self.tools.get(name)
        if tool is None:
            return {"ok": False, "reason": f"未注册工具: {name}"}

        # 1) pre-execute：waterfall。任何一个返回非 allow 即短路。
        #    对应 events.ts 注释：missing approval support turns ask into denial。
        for hook in self.pre_hooks:
            decision = hook(tool, raw_args)
            if callable(getattr(decision, "__await__", None)):
                decision = await decision
            if decision == DECISION_ALLOW:
                continue
            if decision == DECISION_DENY:
                return {"ok": False, "reason": f"pre-execute 拒绝调用 {name}"}
            if decision == DECISION_ASK:
                return {"ok": False, "reason": f"pre-execute 需要人工确认: {name}"}
            if decision == DECISION_CANCEL:
                return {"ok": False, "reason": f"pre-execute 取消调用 {name}"}
            return {"ok": False, "reason": f"未知决策: {decision}"}

        # 2) execute：先在【内部第一行】校验参数，再执行（对应 defineTool 包装）。
        try:
            args = tool.validate(raw_args)
        except ToolArgsError as e:
            return {"ok": False, "reason": f"参数非法: {e.violations}"}

        try:
            result = tool.execute_fn(args)
            if callable(getattr(result, "__await__", None)):
                result = await result
        except Exception as e:  # 工具自身抛错，也进入 post-execute（events.ts: thrown tools still reach post）
            result = {"__error__": str(e)}

        # 3) post-execute：waterfall，可 accept/replace/block。
        for hook in self.post_hooks:
            new = hook(tool, args, result)
            if callable(getattr(new, "__await__", None)):
                new = await new
            if new is not None:
                result = new

        # 4) tools/result：emit（observe-only）。改这里的返回值不影响上面。
        for hook in self.result_hooks:
            hook(tool, args, result)

        if isinstance(result, dict) and "__error__" in result:
            return {"ok": False, "reason": result["__error__"]}
        return {"ok": True, "value": result}


def define_tool(name, description, args_model, execute_fn, output_render=None) -> Tool:
    """对应 dsh 的 defineTool（schema.ts 第 555 行）。"""
    return Tool(name, description, args_model, execute_fn, output_render)


# ── 自测 ──────────────────────────────────────────────────
if __name__ == "__main__":
    import asyncio
    from pydantic import Field

    class GreetArgs(BaseModel):
        name: str = Field(description="要打招呼的人的名字")

    def greet(args: GreetArgs) -> str:
        return f"Hello, {args.name}!"

    registry = ToolRegistry()
    registry.register(define_tool("greet", "Greet someone by name.", GreetArgs, greet))

    # 1) schema 单一来源：既喂模型又校验
    assert registry.tools["greet"].json_schema()["parameters"]["properties"]["name"]["type"] == "string"

    # 2) pre-execute 拦截：名字带「危险」就拒绝
    #    注意：pre-execute 拿到的是【未校验的 raw_args】（校验在 execute 内部），
    #    所以钩子必须对畸形输入健壮——name 可能是 int 而不是 str。
    def deny_danger(tool, raw_args):
        name = raw_args.get("name", "") if isinstance(raw_args, dict) else ""
        return DECISION_DENY if isinstance(name, str) and "危险" in name else DECISION_ALLOW

    registry.pre_hooks.append(deny_danger)

    async def main():
        r1 = await registry.execute("greet", {"name": "Ada"})
        r2 = await registry.execute("greet", {"name": "危险分子"})
        # 3) 参数非法 -> execute 第一行校验拦截
        r3 = await registry.execute("greet", {"name": 123})

        print("正常调用:", r1)
        print("被拒绝调用:", r2)
        print("参数非法:", r3)

        assert r1 == {"ok": True, "value": "Hello, Ada!"}
        assert r2["ok"] is False and "拒绝" in r2["reason"]
        assert r3["ok"] is False and "参数非法" in r3["reason"]
        print("通过：pre-execute 拦截 + execute 第一行校验（fail-closed 的校验版）")

    asyncio.run(main())
