import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, mock_open, patch

import pytest

from litellm.llms.github_copilot.authenticator import (
    Authenticator,
    _should_refresh_api_key,
)
from litellm.llms.github_copilot.common_utils import (
    APIKeyExpiredError,
    GetAccessTokenError,
    GetAPIKeyError,
    GetDeviceCodeError,
    RefreshAPIKeyError,
)


class TestGitHubCopilotAuthenticator:
    @pytest.fixture
    def authenticator(self):
        with (
            patch("os.path.exists", return_value=False),
            patch("os.makedirs") as mock_makedirs,
        ):
            auth = Authenticator()
            mock_makedirs.assert_called_once()
            return auth

    @pytest.fixture
    def mock_http_client(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.post.return_value = mock_response
        mock_response.raise_for_status.return_value = None
        return mock_client, mock_response

    def test_init(self):
        """Test the initialization of the authenticator."""
        with (
            patch("os.path.exists", return_value=False),
            patch("os.makedirs") as mock_makedirs,
        ):
            auth = Authenticator()
            assert auth.token_dir.endswith("/github_copilot")
            assert auth.access_token_file.endswith("/access-token")
            assert auth.api_key_file.endswith("/api-key.json")
            mock_makedirs.assert_called_once()

    def test_ensure_token_dir(self):
        """Test that the token directory is created if it doesn't exist."""
        with (
            patch("os.path.exists", return_value=False),
            patch("os.makedirs") as mock_makedirs,
        ):
            auth = Authenticator()
            mock_makedirs.assert_called_once_with(auth.token_dir, exist_ok=True)

    def test_get_github_headers(self, authenticator):
        """Test that GitHub headers are correctly generated."""
        headers = authenticator._get_github_headers()
        assert "accept" in headers
        assert "editor-version" in headers
        assert "user-agent" in headers
        assert "content-type" in headers

        headers_with_token = authenticator._get_github_headers("test-token")
        assert headers_with_token["authorization"] == "token test-token"

    def test_get_access_token_from_file(self, authenticator):
        """Test retrieving an access token from a file."""
        mock_token = "mock-access-token"

        with patch("builtins.open", mock_open(read_data=mock_token)):
            token = authenticator.get_access_token()
            assert token == mock_token

    def test_get_access_token_login(self, authenticator):
        """Test logging in to get an access token."""
        mock_token = "mock-access-token"

        with (
            patch.object(authenticator, "_login", return_value=mock_token),
            patch("builtins.open", mock_open()),
            patch("builtins.open", side_effect=IOError) as mock_read,
        ):
            token = authenticator.get_access_token()
            assert token == mock_token
            authenticator._login.assert_called_once()

    def test_get_access_token_failure(self, authenticator):
        """Test that an exception is raised after multiple login failures."""
        with (
            patch.object(
                authenticator,
                "_login",
                side_effect=GetDeviceCodeError(message="Test error", status_code=400),
            ),
            patch("builtins.open", side_effect=IOError),
        ):
            with pytest.raises(GetAccessTokenError):
                authenticator.get_access_token()
            assert authenticator._login.call_count == 3

    def test_get_api_key_from_file(self, authenticator):
        """Test retrieving an API key from a file."""
        future_time = (datetime.now() + timedelta(hours=1)).timestamp()
        mock_api_key_data = json.dumps(
            {"token": "mock-api-key", "expires_at": future_time}
        )

        with patch("builtins.open", mock_open(read_data=mock_api_key_data)):
            api_key = authenticator.get_api_key()
            assert api_key == "mock-api-key"

    def test_get_api_key_expired(self, authenticator):
        """Test refreshing an expired API key."""
        past_time = (datetime.now() - timedelta(hours=1)).timestamp()
        mock_expired_data = json.dumps(
            {"token": "expired-api-key", "expires_at": past_time}
        )
        mock_new_data = {
            "token": "new-api-key",
            "expires_at": (datetime.now() + timedelta(hours=1)).timestamp(),
        }

        with (
            patch("builtins.open", mock_open(read_data=mock_expired_data)),
            patch.object(authenticator, "_refresh_api_key", return_value=mock_new_data),
            patch("json.dump") as mock_json_dump,
        ):
            api_key = authenticator.get_api_key()
            assert api_key == "new-api-key"
            authenticator._refresh_api_key.assert_called_once()

    def test_get_api_key_refreshes_at_refresh_in_before_expiry(self, authenticator):
        """Proactive refresh: once refresh_in seconds have elapsed since the token
        was obtained, refresh even though expires_at is still in the future.

        Regression: the key used to be refreshed only after it had actually
        expired, so the request that hit the expiry boundary paid the refresh
        latency and logged a warning every ~30 min.
        """
        now = datetime.now().timestamp()
        # Obtained 26 min ago; refresh_in = 25 min -> past the refresh point, yet
        # expires_at is 4 min in the future so the key is technically still valid.
        stale_but_valid = json.dumps(
            {
                "token": "stale-api-key",
                "expires_at": now + 4 * 60,
                "refresh_in": 25 * 60,
                "last_refreshed": now - 26 * 60,
            }
        )
        new_data = {"token": "fresh-api-key", "expires_at": now + 30 * 60, "refresh_in": 25 * 60}

        with (
            patch("builtins.open", mock_open(read_data=stale_but_valid)),
            patch.object(authenticator, "_refresh_api_key", return_value=new_data) as mock_refresh,
            patch("json.dump"),
        ):
            assert authenticator.get_api_key() == "fresh-api-key"
            mock_refresh.assert_called_once()

    def test_get_api_key_not_refreshed_before_refresh_in(self, authenticator):
        """Stay on the cached key while still inside the refresh_in window; do not
        refresh on every request just because refresh_in exists."""
        now = datetime.now().timestamp()
        fresh = json.dumps(
            {
                "token": "current-api-key",
                "expires_at": now + 30 * 60,
                "refresh_in": 25 * 60,
                "last_refreshed": now - 5 * 60,  # only 5 min old; refresh point is 25 min
            }
        )

        with (
            patch("builtins.open", mock_open(read_data=fresh)),
            patch.object(authenticator, "_refresh_api_key") as mock_refresh,
        ):
            assert authenticator.get_api_key() == "current-api-key"
            mock_refresh.assert_not_called()

    def test_get_api_key_uses_file_mtime_when_last_refreshed_missing(self, authenticator):
        """Tokens written by external tools lack last_refreshed; fall back to the
        file mtime so they still refresh proactively at the refresh_in point."""
        now = datetime.now().timestamp()
        external = json.dumps(
            {
                "token": "external-api-key",
                "expires_at": now + 4 * 60,  # still valid
                "refresh_in": 25 * 60,
                # no last_refreshed field
            }
        )
        new_data = {"token": "fresh-api-key", "expires_at": now + 30 * 60, "refresh_in": 25 * 60}

        with (
            patch("builtins.open", mock_open(read_data=external)),
            patch("os.path.getmtime", return_value=now - 26 * 60),  # obtained 26 min ago
            patch.object(authenticator, "_refresh_api_key", return_value=new_data) as mock_refresh,
            patch("json.dump"),
        ):
            assert authenticator.get_api_key() == "fresh-api-key"
            mock_refresh.assert_called_once()

    def test_get_api_key_persists_last_refreshed_on_refresh(self, authenticator):
        """After refreshing, last_refreshed must be persisted (alongside the
        upstream fields) so the next read computes the refresh point without
        relying on the file mtime."""
        now = datetime.now().timestamp()
        expired = json.dumps({"token": "old", "expires_at": now - 60})
        new_data = {"token": "new", "expires_at": now + 30 * 60, "refresh_in": 1500}

        with (
            patch("builtins.open", mock_open(read_data=expired)),
            patch.object(authenticator, "_refresh_api_key", return_value=new_data),
            patch("json.dump") as mock_dump,
        ):
            assert authenticator.get_api_key() == "new"
            written = mock_dump.call_args[0][0]
            assert "last_refreshed" in written
            assert written["token"] == "new"
            assert written["refresh_in"] == 1500  # upstream fields preserved

    def test_refresh_api_key(self, authenticator, mock_http_client):
        """Test refreshing an API key."""
        mock_client, mock_response = mock_http_client
        mock_token = "mock-access-token"
        mock_api_key_data = {"token": "new-api-key", "expires_at": 12345}

        with (
            patch.object(authenticator, "get_access_token", return_value=mock_token),
            patch(
                "litellm.llms.github_copilot.authenticator._get_httpx_client",
                return_value=mock_client,
            ),
            patch.object(mock_response, "json", return_value=mock_api_key_data),
        ):
            result = authenticator._refresh_api_key()
            assert result == mock_api_key_data
            mock_client.get.assert_called_once()
            authenticator.get_access_token.assert_called_once()

    def test_refresh_api_key_failure(self, authenticator, mock_http_client):
        """Test failure to refresh an API key."""
        mock_client, mock_response = mock_http_client
        mock_token = "mock-access-token"

        with (
            patch.object(authenticator, "get_access_token", return_value=mock_token),
            patch(
                "litellm.llms.github_copilot.authenticator._get_httpx_client",
                return_value=mock_client,
            ),
            patch.object(mock_response, "json", return_value={}),
        ):
            with pytest.raises(RefreshAPIKeyError):
                authenticator._refresh_api_key()
            assert mock_client.get.call_count == 3

    def test_get_device_code(self, authenticator, mock_http_client):
        """Test getting a device code."""
        mock_client, mock_response = mock_http_client
        mock_device_code_data = {
            "device_code": "mock-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
        }

        with (
            patch(
                "litellm.llms.github_copilot.authenticator._get_httpx_client",
                return_value=mock_client,
            ),
            patch.object(mock_response, "json", return_value=mock_device_code_data),
        ):
            result = authenticator._get_device_code()
            assert result == mock_device_code_data
            mock_client.post.assert_called_once()

    def test_poll_for_access_token(self, authenticator, mock_http_client):
        """Test polling for an access token."""
        mock_client, mock_response = mock_http_client
        mock_token_data = {"access_token": "mock-access-token"}

        with (
            patch(
                "litellm.llms.github_copilot.authenticator._get_httpx_client",
                return_value=mock_client,
            ),
            patch.object(mock_response, "json", return_value=mock_token_data),
            patch("time.sleep"),
        ):
            result = authenticator._poll_for_access_token("mock-device-code")
            assert result == "mock-access-token"
            mock_client.post.assert_called_once()

    def test_login(self, authenticator):
        """Test the login process."""
        mock_device_code_data = {
            "device_code": "mock-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
        }
        mock_token = "mock-access-token"

        with (
            patch.object(
                authenticator, "_get_device_code", return_value=mock_device_code_data
            ),
            patch.object(
                authenticator, "_poll_for_access_token", return_value=mock_token
            ),
            patch("builtins.print") as mock_print,
        ):
            result = authenticator._login()
            assert result == mock_token
            authenticator._get_device_code.assert_called_once()
            authenticator._poll_for_access_token.assert_called_once_with(
                "mock-device-code"
            )
            mock_print.assert_called_once()

    def test_get_api_base_from_file(self, authenticator):
        """Test retrieving the API base endpoint from a file."""
        mock_api_key_data = json.dumps(
            {
                "token": "mock-api-key",
                "expires_at": (datetime.now() + timedelta(hours=1)).timestamp(),
                "endpoints": {"api": "https://api.enterprise.githubcopilot.com"},
            }
        )
        with patch("builtins.open", mock_open(read_data=mock_api_key_data)):
            api_base = authenticator.get_api_base()
            assert api_base == "https://api.enterprise.githubcopilot.com"

    def test_get_device_code_with_custom_url(self, authenticator, mock_http_client):
        """GITHUB_COPILOT_DEVICE_CODE_URL env var must be used by _get_device_code at call time."""
        mock_client, mock_response = mock_http_client
        custom_url = "https://custom.example.com/device"
        mock_response.json.return_value = {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://example.com",
        }
        with patch.dict(os.environ, {"GITHUB_COPILOT_DEVICE_CODE_URL": custom_url}), \
             patch("litellm.llms.github_copilot.authenticator._get_httpx_client", return_value=mock_client):
            authenticator._get_device_code()
            assert mock_client.post.call_args[0][0] == custom_url

    def test_get_device_code_with_custom_client_id(self, authenticator, mock_http_client):
        """GITHUB_COPILOT_CLIENT_ID env var must appear as client_id in the device-code request body."""
        mock_client, mock_response = mock_http_client
        custom_id = "custom_client_id"
        mock_response.json.return_value = {
            "device_code": "dc",
            "user_code": "UC",
            "verification_uri": "https://example.com",
        }
        with patch.dict(os.environ, {"GITHUB_COPILOT_CLIENT_ID": custom_id}), \
             patch("litellm.llms.github_copilot.authenticator._get_httpx_client", return_value=mock_client):
            authenticator._get_device_code()
            assert mock_client.post.call_args[1]["json"]["client_id"] == custom_id

    def test_poll_for_access_token_with_custom_url(self, authenticator, mock_http_client):
        """GITHUB_COPILOT_ACCESS_TOKEN_URL env var must be used by _poll_for_access_token at call time."""
        mock_client, mock_response = mock_http_client
        custom_url = "https://custom.example.com/token"
        mock_response.json.return_value = {"access_token": "tok"}
        with patch.dict(os.environ, {"GITHUB_COPILOT_ACCESS_TOKEN_URL": custom_url}), \
             patch("litellm.llms.github_copilot.authenticator._get_httpx_client", return_value=mock_client), \
             patch("time.sleep"):
            authenticator._poll_for_access_token("dc")
            assert mock_client.post.call_args[0][0] == custom_url

    def test_poll_for_access_token_with_custom_client_id(self, authenticator, mock_http_client):
        """GITHUB_COPILOT_CLIENT_ID env var must appear as client_id in the polling request body."""
        mock_client, mock_response = mock_http_client
        custom_id = "custom_client_id"
        mock_response.json.return_value = {"access_token": "tok"}
        with patch.dict(os.environ, {"GITHUB_COPILOT_CLIENT_ID": custom_id}), \
             patch("litellm.llms.github_copilot.authenticator._get_httpx_client", return_value=mock_client), \
             patch("time.sleep"):
            authenticator._poll_for_access_token("dc")
            assert mock_client.post.call_args[1]["json"]["client_id"] == custom_id

    def test_refresh_api_key_with_custom_url(self, authenticator, mock_http_client):
        """GITHUB_COPILOT_API_KEY_URL env var must be used by _refresh_api_key at call time."""
        mock_client, mock_response = mock_http_client
        custom_url = "https://custom.example.com/api-key"
        mock_response.json.return_value = {"token": "api-tok", "expires_at": 9999999999}
        with patch.dict(os.environ, {"GITHUB_COPILOT_API_KEY_URL": custom_url}), \
             patch("litellm.llms.github_copilot.authenticator._get_httpx_client", return_value=mock_client), \
             patch.object(authenticator, "get_access_token", return_value="access-tok"):
            authenticator._refresh_api_key()
            assert mock_client.get.call_args[0][0] == custom_url


class TestShouldRefreshAPIKey:
    """Unit tests for the proactive-refresh decision, independent of file I/O."""

    def test_refreshes_exactly_at_refresh_in_point(self):
        # obtained at t=0, refresh_in=1500 -> due at t>=1500, well before expiry at 1800
        assert _should_refresh_api_key(now=1500, expires_at=1800, refresh_in=1500, obtained_at=0) is True

    def test_no_refresh_one_second_before_refresh_in_point(self):
        assert _should_refresh_api_key(now=1499, expires_at=1800, refresh_in=1500, obtained_at=0) is False

    def test_fallback_to_expiry_when_no_refresh_in(self):
        # No refresh_in hint -> lazy expiry-based behaviour is preserved.
        assert _should_refresh_api_key(now=1799, expires_at=1800, refresh_in=None, obtained_at=0) is False
        assert _should_refresh_api_key(now=1800, expires_at=1800, refresh_in=None, obtained_at=0) is True

    def test_fallback_to_expiry_when_obtained_at_unknown(self):
        # refresh_in present but no obtained_at (unreadable mtime) -> expiry-based.
        assert _should_refresh_api_key(now=1799, expires_at=1800, refresh_in=1500, obtained_at=None) is False
        assert _should_refresh_api_key(now=1800, expires_at=1800, refresh_in=1500, obtained_at=None) is True

