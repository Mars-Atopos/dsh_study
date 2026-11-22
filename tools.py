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