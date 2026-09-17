"""Unit tests for database connection management, credential sanitization, and error handling."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch
import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.connection import (
    DatabaseManager,
    redact_database_url,
    sanitize_error_message,
)
from src.scd2_copilot.db.exceptions import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseTransactionError,
)


class TestCredentialSanitization:
    """Verify that credentials are NEVER leaked in URLs or error messages."""

    def test_redact_database_url_standard(self) -> None:
        raw = "postgresql://myuser:supersecretpass@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        redacted = redact_database_url(raw)
        assert "supersecretpass" not in redacted
        assert "myuser:***@aws-0-ap-south-1.pooler.supabase.com" in redacted

    def test_redact_database_url_special_characters(self) -> None:
        raw = "postgresql://user%40example.com:p%40ss%3Aw%23rd@db.supabase.co:5432/prod_db"
        redacted = redact_database_url(raw)
        assert "p%40ss%3Aw%23rd" not in redacted
        assert ":***@" in redacted

    def test_redact_database_url_no_password(self) -> None:
        raw = "postgresql://localhost:5432/testdb"
        assert redact_database_url(raw) == raw

    def test_redact_database_url_empty_or_none(self) -> None:
        assert redact_database_url(None) == ""
        assert redact_database_url("") == ""

    def test_sanitize_error_message_with_passwords(self) -> None:
        err = "FATAL: password authentication failed for user 'postgres.xyz' with password 'my_secret_token_123'"
        sanitized = sanitize_error_message(err)
        assert "my_secret_token_123" not in sanitized
        assert "password '***'" in sanitized

    def test_sanitize_error_message_with_connection_url(self) -> None:
        err = "could not connect to postgresql://user:mypassword123@aws-0-ap-south-1.pooler.supabase.com:5432/db"
        sanitized = sanitize_error_message(err)
        assert "mypassword123" not in sanitized
        assert "user:***@" in sanitized


class TestDatabaseManagerConfig:
    """Verify configuration validation and error behavior when connection is unconfigured."""

    def test_missing_database_url_raises_configuration_error(self) -> None:
        settings = Settings(database_url=None)
        db_mgr = DatabaseManager(settings=settings)
        assert not db_mgr.is_configured
        assert db_mgr.redacted_url == ""

        with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL is not configured"):
            with db_mgr.get_connection():
                pass

    def test_configured_properties_redacted(self) -> None:
        settings = Settings(database_url="postgresql://usr:secret_pw@host:5432/db")
        db_mgr = DatabaseManager(settings=settings)
        assert db_mgr.is_configured
        assert "secret_pw" not in db_mgr.redacted_url
        assert "usr:***@host:5432/db" in db_mgr.redacted_url


class TestDatabaseManagerConnectionHandling:
    """Verify error wrapping during connection and query failures."""

    def test_connection_failure_wraps_in_database_connection_error(self) -> None:
        settings = Settings(
            database_url="postgresql://bad_user:bad_pass@127.0.0.1:59999/nonexistent",
            db_connect_timeout=1,
        )
        db_mgr = DatabaseManager(settings=settings)

        with pytest.raises(DatabaseConnectionError) as exc_info:
            with db_mgr.get_connection():
                pass
        assert "bad_pass" not in str(exc_info.value)

    def test_health_check_raises_on_unreachable_host(self) -> None:
        settings = Settings(
            database_url="postgresql://bad_user:bad_pass@127.0.0.1:59999/nonexistent",
            db_connect_timeout=1,
        )
        db_mgr = DatabaseManager(settings=settings)
        assert db_mgr.ping() is False
        with pytest.raises(DatabaseConnectionError):
            db_mgr.health_check()

    def test_health_check_raises_when_unconfigured(self) -> None:
        settings = Settings(database_url=None)
        db_mgr = DatabaseManager(settings=settings)
        assert db_mgr.ping() is False
        with pytest.raises(DatabaseConfigurationError):
            db_mgr.health_check()


class TestTransactionManagement:
    """Verify atomic transaction behavior and error handling."""

    def test_transaction_rolls_back_on_error(self) -> None:
        mock_conn = MagicMock()
        mock_tx = MagicMock()
        mock_conn.transaction.return_value.__enter__.return_value = mock_tx

        db_mgr = DatabaseManager(settings=Settings(database_url="postgresql://u:p@h:5432/d"))

        with patch.object(db_mgr, "get_connection") as mock_get_conn:
            mock_get_conn.return_value.__enter__.return_value = mock_conn

            with pytest.raises(DatabaseTransactionError, match="Transaction failed"):
                with db_mgr.transaction() as conn:
                    raise ValueError("Simulated pipeline failure")

            # Verify transaction context manager was used
            assert mock_conn.transaction.called
