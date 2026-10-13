            except Exception as exc:
                logger.exception(
                    "Periodic preset %r: initial turn failed (session %s)",
                    name,
                    session_id,
                )
                # Emit a partial-failure report into the transcript so future
                # runs can see what was attempted and where it failed. The
                # session is deliberately NOT completion-closed here — a
                # failed run is left for the supersede-close safety net.
                if self._report_partial_result is not None:
                    error_message = _format_partial_error_report(name, exc)
                    try:
                        await self._report_partial_result(session_id, error_message)
                    except Exception:
                        logger.exception(
                            "Periodic preset %r: emitting partial-failure "
                            "report for session %s failed",
                            name,
                            session_id,
                        )
                # CRITICAL: Subsession visibility is independent of main-task
                # success. Even if the turn failed, any open user_chat
                # subsessions (operator decisions awaiting reply) must be
                # surfaced in the transcript. This fallback ensures the
                # operator always sees pending decisions.
                if self._report_pending_subsessions is not None:
                    try:
                        await self._report_pending_subsessions(session_id)
                    except Exception:
                        logger.exception(
                            "Periodic preset %r: fallback subsession visibility "
                            "check for session %s failed",
                            name,
                            session_id,
                        )
