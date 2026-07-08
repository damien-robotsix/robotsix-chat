Bump the `robotsix-llmio` pin to pick up the Claude Agent SDK per-call wall-clock cap raise (20min
-> 1h) — a genuine multi-turn tool loop was tripping the old cap under host load.
