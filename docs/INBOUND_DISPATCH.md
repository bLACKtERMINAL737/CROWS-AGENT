# Durable inbound dispatch journal

Verified provider webhooks are first persisted in the normalized messaging inbox.
The inbound dispatch journal then records the channel-scoped agent session selected
for each inbox message. Its unique message constraint makes enqueue idempotent.

Dispatch workers must claim rows with a bounded lease and finish only while they
own a live lease. A worker crash or expired lease moves the row to `unknown`;
unknown work is deliberately not replayed automatically because the agent may
already have completed model or tool side effects before the worker stopped.

This journal is a durable boundary, not permission to execute arbitrary tools.
The runtime integration must apply channel policy and tool permissions before
execution. The initial implementation exposes queue state without returning
message text, provider identifiers, prompts, or tool arguments.
