# Limited multi-agent boundary

`broker.py` is a retained experimental in-memory queue, no longer wired into
the API. Durable request orchestration now lives in `runtime/manager.py` and
`runtime/store.py`, with per-session ordering and two execution slots. Candidate, Market,
Growth, and Application are currently logical lanes handled by the main Agent,
not independently running subagents.

Future subagents should exchange typed tasks through the durable runtime. They may analyze
in parallel, but they may not manipulate the same browser session concurrently.
Application actions remain behind deterministic policy and human authorization.
