Made `consult_mill` (and the other brokered agents) resilient to a slow or unreachable board
manager. A fast pre-flight reachability check (authenticated `GET /agents` with a short timeout) now
runs before each request, so a down broker or an offline recipient fails in a few seconds instead of
hanging for the full request timeout. Because the board manager is a multi-turn LLM agent that
legitimately takes tens of seconds — longer when its replies queue behind other mill work — the mill
request timeout was raised from 120s to 300s (`MILL_TIMEOUT`). Net effect: genuine outages surface
quickly, while a reachable-but-busy board manager is given room to finish instead of spuriously
timing out.
