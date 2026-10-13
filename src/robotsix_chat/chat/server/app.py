        async def _periodic_prune_closed(keep: set[str]) -> int:
            """Delete closed periodic runs outside the retention window."""
            return prune_closed_periodic_sessions(
                _p_store, keep, registry=app.state.subsession_registry
            )

        async def _periodic_report_pending_subsessions(session_id: str) -> None:
            """Surface any open user_chat subsessions awaiting operator reply.

            Called as a fallback when the main periodic turn fails, ensuring
            open decision panels are visible in the transcript even if the
            task crashes. This makes subsession visibility independent from
            task success, so pending operator decisions are never hidden.
            """
            registry = app.state.subsession_registry
            if registry is None:
                return
            open_user_chats = [
                sub
                for sub in registry.list_for_owner(session_id)
                if sub.kind == "user_chat" and sub.is_active
            ]
            if not open_user_chats:
                return
            # Format subsession list for the transcript
            subsession_report = "SUBSESSION VISIBILITY CHECK (fallback — main turn failed)\n\n"
            subsession_report += f"Open user_chat panels awaiting operator reply: {len(open_user_chats)}\n\n"
            for sub in open_user_chats:
                subsession_report += f"- {sub.id} ({sub.title})\n"
                if sub.status:
                    subsession_report += f"  Status: {sub.status}\n"
            subsession_report += (
                "\nThese panels remain open and visible in the sidebar. "
                "The operator can still interact with them to make pending decisions."
            )
            _p_store.record(
                session_id,
                PERIODIC_OWNER,
                "(subsession visibility fallback)",
                subsession_report,
            )

        app.state.periodic_scheduler = PeriodicScheduler(
            definitions=periodic_definitions,
            conversation_store=_p_store,
            submit_turn=_periodic_submit_turn,
            is_busy=_p_coalescer.is_busy,
            persist_path=periodic_state_path or PERIODIC_SCHEDULER_PERSIST_PATH,
            close_previous=_periodic_close_previous,
            close_completed=_periodic_close_completed,
            has_live_subsessions=_periodic_has_live_subsessions,
            report_partial_result=_periodic_report_partial_result,
            ensure_partial_report_on_interruption=_periodic_ensure_partial_report,
            carry_over_subsessions=_periodic_carry_over,
            prune_closed_runs=_periodic_prune_closed,
            report_pending_subsessions=_periodic_report_pending_subsessions,
        )
