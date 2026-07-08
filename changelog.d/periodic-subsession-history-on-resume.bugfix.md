Fix periodic subsessions losing all accumulated context on every chat restart. A subsession worker's
conversation history (`history: list = []`) was reinitialized from scratch whenever its worker
restarted — including when a long-running periodic subsession (e.g. a board-monitoring loop) was
resumed after a deploy — so it had no memory of anything from prior runs. When such a subsession
then spawned a nested subsession (for example to ask the operator a decision), it couldn't
accurately convey what had already been asked or decided, forcing repeat questions and pushing the
nested agent to lean on memory recall instead of real context. Each turn's (input, reply) pair is
now persisted (`turn_history`, capped like the existing transcript) and replayed to seed the
worker's history when a periodic subsession resumes, so it picks up where it left off instead of
starting blank.
