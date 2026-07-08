The typing indicator now shows a `recall_memory` step while the agent searches prior conversation
context, before the Claude SDK turn even starts. Memory recall runs first in every turn and has been
observed taking 90+ seconds on its own — previously that whole phase showed nothing but blank dots,
with no visible activity until the SDK subprocess itself started reporting tool calls.
