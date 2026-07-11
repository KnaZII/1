"""Tests for diagnostics module — redaction, formatting, cert parsing, geo-blocking."""

from __future__ import annotations

from meridian.commands.diagnostics import (
    _check_cert_expiry,
    _check_geo_blocking,
    _format_sections,
    _redact_secrets,
)


class TestRedactSecrets:
    """Verify secret redaction patterns."""

    def test_redacts_uuid(self) -> None:
        text = "client id: 550e8400-e29b-41d4-a716-446655440000"
        assert "[UUID-REDACTED]" in _redact_secrets(text)
        assert "550e8400" not in _redact_secrets(text)

    def test_redacts_password(self) -> None:
        text = "Password: s3cret!pass"
        result = _redact_secrets(text)
        assert "s3cret" not in result
        assert "Password=[REDACTED]" in result

    def test_redacts_key(self) -> None:
        text = "Key=WBNp7SHzGMaqp6ohXMfJHUy"
        result = _redact_secrets(text)
        assert "WBNp7" not in result
        assert "Key=[REDACTED]" in result

    def test_preserves_non_secret_text(self) -> None:
        text = "nginx started on port 443"
        assert _redact_secrets(text) == text


class TestFormatSections:
    """Verify markdown formatting."""

    def test_formats_as_markdown(self) -> None:
        sections = [("Title", "body text")]
        result = _format_sections(sections)
        assert "### Title" in result
        assert "```\nbody text\n```" in result

    def test_multiple_sections(self) -> None:
        sections = [("A", "1"), ("B", "2")]
        result = _format_sections(sections)
        assert "### A" in result
        assert "### B" in result


class TestCheckCertExpiry:
    """Verify TLS certificate expiry parsing."""

    def test_valid_cert(self) -> None:
        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout="notAfter=Dec 31 23:59:59 2030 GMT", returncode=0)

        result = _check_cert_expiry(FakeConn())
        assert "valid until 2030-12-31" in result
        assert "days" in result

    def test_expired_cert(self) -> None:
        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout="notAfter=Jan 01 00:00:00 2020 GMT", returncode=0)

        result = _check_cert_expiry(FakeConn())
        assert "EXPIRED" in result

    def test_no_cert(self) -> None:
        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout="", returncode=1)

        result = _check_cert_expiry(FakeConn())
        assert "could not check" in result


class TestCheckGeoBlocking:
    """Verify geo-blocking status detection."""

    def test_active_geo_blocking(self) -> None:
        import json

        config = {
            "outbounds": [{"protocol": "blackhole", "tag": "blocked"}],
            "routing": {
                "rules": [
                    {"type": "field", "outboundTag": "blocked", "domain": ["geosite:category-ru"]},
                    {"type": "field", "outboundTag": "blocked", "ip": ["geoip:ru"]},
                ]
            },
        }

        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout=json.dumps(config), returncode=0)

        result = _check_geo_blocking(FakeConn())
        assert "active" in result
        assert "2 rules" in result

    def test_not_configured(self) -> None:
        import json

        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout=json.dumps({"outbounds": []}), returncode=0)

        result = _check_geo_blocking(FakeConn())
        assert "not configured" in result

    def test_empty_config(self) -> None:
        class FakeConn:
            def run(self, cmd: str, timeout: int = 10) -> object:
                from types import SimpleNamespace

                return SimpleNamespace(stdout="", returncode=1)

        result = _check_geo_blocking(FakeConn())
        assert "could not read" in result
