"""Tests for health check models."""

from __future__ import annotations

import time

from robotsix_chat.health.models import CheckResult, CheckSeverity, HealthStatus


class TestCheckSeverity:
    """Tests for :class:`CheckSeverity`."""

    def test_ordering(self) -> None:
        """Severity values match their string representations."""
        assert CheckSeverity.OK == "ok"
        assert CheckSeverity.WARNING == "warning"
        assert CheckSeverity.ERROR == "error"


class TestCheckResult:
    """Tests for :class:`CheckResult`."""

    def test_defaults(self) -> None:
        """Default values are sensible."""
        result = CheckResult(name="test", status=CheckSeverity.OK)
        assert result.name == "test"
        assert result.status == CheckSeverity.OK
        assert result.message == ""
        assert result.details == {}
        assert result.timestamp > 0

    def test_full(self) -> None:
        """All fields can be set explicitly."""
        ts = time.monotonic()
        result = CheckResult(
            name="memory",
            status=CheckSeverity.ERROR,
            message="degraded",
            details={"backend": "cognee"},
            timestamp=ts,
        )
        assert result.name == "memory"
        assert result.status == CheckSeverity.ERROR
        assert result.message == "degraded"
        assert result.details == {"backend": "cognee"}
        assert result.timestamp == ts


class TestHealthStatus:
    """Tests for :class:`HealthStatus`."""

    def test_defaults(self) -> None:
        """Default values are sensible."""
        status = HealthStatus()
        assert status.checks == []
        assert status.last_run == 0.0
        assert status.overall == CheckSeverity.OK

    def test_with_checks(self) -> None:
        """Checks and overall status are stored correctly."""
        checks = [
            CheckResult(name="a", status=CheckSeverity.OK),
            CheckResult(name="b", status=CheckSeverity.ERROR, message="fail"),
        ]
        status = HealthStatus(checks=checks, overall=CheckSeverity.ERROR)
        assert len(status.checks) == 2
        assert status.overall == CheckSeverity.ERROR
