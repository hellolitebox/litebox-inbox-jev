"""OAuth token files: written atomically and readable by their owner only, from the start."""

import json
import os
import stat
from unittest import mock

import google.auth.transport
import pytest

from inbox_jev import files as files_module
from inbox_jev import gmail as gmail_module
from inbox_jev.files import write_private
from inbox_jev.gmail import load_credentials, token_path

_STORED = {
    "refresh_token": "r",
    "client_id": "i",
    "client_secret": "s",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _leftovers(directory, keep):
    return sorted(p.name for p in directory.iterdir() if p.name != keep)


def test_write_private_creates_an_owner_only_file(tmp_path):
    path = tmp_path / "me@acme.com.json"
    write_private(path, '{"token": "a"}')
    assert path.read_text() == '{"token": "a"}'
    assert _mode(path) == 0o600
    assert _leftovers(tmp_path, path.name) == []


def test_write_private_replaces_a_readable_file_with_an_owner_only_one(tmp_path):
    path = tmp_path / "me@acme.com.json"
    path.write_text('{"token": "old"}')
    path.chmod(0o644)  # e.g. written by an older version, or by hand
    write_private(path, '{"token": "new"}')
    assert path.read_text() == '{"token": "new"}'
    assert _mode(path) == 0o600


def test_write_private_never_exposes_the_token_while_writing(tmp_path, monkeypatch):
    """The temporary file is already 0600 when the token is written into it."""
    path = tmp_path / "me@acme.com.json"
    real_fdopen = os.fdopen
    modes = []

    def fdopen(fd, *args, **kwargs):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(files_module.os, "fdopen", fdopen)
    write_private(path, "{}")
    assert modes == [0o600]


def test_failed_save_keeps_the_previous_token_and_no_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "me@acme.com.json"
    path.write_text('{"token": "old"}')

    def replace(self, target):
        raise OSError("disk full")

    monkeypatch.setattr(files_module.Path, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        write_private(path, '{"token": "new"}')
    assert path.read_text() == '{"token": "old"}'
    assert _leftovers(tmp_path, path.name) == []


def test_write_private_tolerates_a_directory_that_cant_be_fsynced(tmp_path, monkeypatch):
    """Some filesystems refuse fsync on a directory (EINVAL): the token is saved anyway."""
    path = tmp_path / "me@acme.com.json"
    real_fsync = os.fsync
    refused = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            refused.append(fd)
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(files_module.os, "fsync", fsync)
    write_private(path, '{"token": "new"}')
    assert refused  # the directory fsync was attempted and refused
    assert path.read_text() == '{"token": "new"}'
    assert _mode(path) == 0o600
    assert _leftovers(tmp_path, path.name) == []


class _TokenEndpoint(google.auth.transport.Request):
    """The OAuth token endpoint answering every refresh with a new access token."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        response = mock.Mock(spec=google.auth.transport.Response)
        response.status, response.headers = 200, {}
        response.data = json.dumps({"access_token": "fresh", "expires_in": 3600}).encode()
        return response


def test_refreshed_token_is_saved_owner_only(tmp_path, monkeypatch):
    path = token_path(tmp_path, "me@acme.com")
    path.write_text(json.dumps(_STORED))  # no access token: refreshed on load
    path.chmod(0o644)
    monkeypatch.setattr(gmail_module, "Request", _TokenEndpoint)
    creds = load_credentials(tmp_path, "me@acme.com")
    assert creds.token == "fresh"
    assert json.loads(path.read_text())["token"] == "fresh"
    assert _mode(path) == 0o600
    assert _leftovers(tmp_path, path.name) == []


class _Flow:
    """The browser flow, done: Google's token response holds `token_response`."""

    def __init__(self, token_response: dict) -> None:
        self.oauth2session = mock.Mock(token=token_response)

    def run_local_server(self, **kwargs):
        return gmail_module.Credentials.from_authorized_user_info({**_STORED, "token": "a"})


def _authorize(tmp_path, monkeypatch, token_response: dict):
    flow = _Flow(token_response)
    monkeypatch.setattr(
        gmail_module.InstalledAppFlow, "from_client_secrets_file", lambda *args: flow
    )
    service = mock.Mock()
    service.users().getProfile().execute.return_value = {"emailAddress": "Me@acme.com"}
    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: service)
    return gmail_module.authorize(tmp_path / "client.json", tmp_path, "me@acme.com")


def test_an_authorization_google_limits_records_when_it_ends(tmp_path, monkeypatch):
    """A Testing app's grant comes with `refresh_token_expires_in` (7 days, ADR 0052)."""
    before = gmail_module.datetime.now(gmail_module.UTC)
    path = _authorize(tmp_path, monkeypatch, {"refresh_token_expires_in": 604799})
    expires = gmail_module.refresh_token_expiry(tmp_path, "me@acme.com")
    assert expires is not None
    assert abs(expires - (before + gmail_module.timedelta(days=7))).total_seconds() < 5
    assert json.loads(path.read_text())["refresh_token"] == "r"
    assert _mode(path) == 0o600


def test_an_authorization_with_no_limit_has_no_end(tmp_path, monkeypatch):
    path = _authorize(tmp_path, monkeypatch, {"access_token": "a", "expires_in": 3599})
    assert gmail_module.REFRESH_EXPIRES_KEY not in json.loads(path.read_text())
    assert gmail_module.refresh_token_expiry(tmp_path, "me@acme.com") is None


def test_a_refresh_keeps_when_the_authorization_ends(tmp_path, monkeypatch):
    path = token_path(tmp_path, "me@acme.com")
    stored = {**_STORED, gmail_module.REFRESH_EXPIRES_KEY: "2026-10-22T10:00:00+00:00"}
    path.write_text(json.dumps(stored))
    monkeypatch.setattr(gmail_module, "Request", _TokenEndpoint)
    load_credentials(tmp_path, "me@acme.com")
    saved = json.loads(path.read_text())
    assert saved["token"] == "fresh"
    assert saved[gmail_module.REFRESH_EXPIRES_KEY] == "2026-10-22T10:00:00+00:00"


@pytest.mark.parametrize(
    "content",
    [None, "{not json", "[]", json.dumps({**_STORED, "refresh_token_expires_at": "soon"})],
)
def test_an_end_that_cant_be_read_is_none(tmp_path, content):
    if content is not None:
        token_path(tmp_path, "me@acme.com").write_text(content)
    assert gmail_module.refresh_token_expiry(tmp_path, "me@acme.com") is None
