#: EnsurePartialReportOnInterruption inspects a completed periodic run's
#: transcript and appends a PARTIAL REPORT if the turn ended abruptly without one.
#: This ensures the next firing inherits context about what was attempted, even if
#: token exhaustion or API errors interrupt the agent before it can emit its report.
EnsurePartialReportOnInterruption = Callable[[str], Awaitable[Any]]

#: ReportPendingSubsessions surfaces any open user_chat subsessions awaiting
#: operator reply in the transcript, ensuring they are visible even if the
#: main periodic turn fails. This makes subsession visibility independent from
#: task success, so pending operator decisions are never hidden by task failures.
ReportPendingSubsessions = Callable[[str], Awaitable[Any]]
