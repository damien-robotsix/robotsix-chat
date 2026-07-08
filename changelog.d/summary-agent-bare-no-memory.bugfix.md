The `POST /summary` agent was built exactly like the main chat agent — full tool suite,
cross-session `ChatMemory` recall, roster/lifecycle instruction augmentation — for what should be a
single bounded text-transformation call over an explicit transcript already in the prompt. In
production, `ChatMemory.recall()` alone was observed taking 90+ seconds, dwarfing the actual
(cheap-tier) model call. `create_agent_from_settings` gains a `bare` flag that skips all of it —
`NullMemory`, no tools, no roster/lifecycle instructions — and the summary agent now uses it.
