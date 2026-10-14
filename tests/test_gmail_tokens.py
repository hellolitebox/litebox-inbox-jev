"""OAuth token files: written atomically and readable by their owner only, from the start."""

import json
import os
import stat
from unittest import mock

import google.auth.transport
import pytest

from inbox_jev import gmail as gmail_module
from inbox_jev.gmail import load_credentials, save_token, token_path

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


def test_save_token_creates_an_owner_only_file(tmp_path):
    path = tmp_path / "me@acme.com.json"
    save_token(path, '{"token": "a"}')
    assert path.read_text() == '{"token": "a"}'
    assert _mode(path) == 0o600
    assert _leftovers(tmp_path, path.name) == []


def test_save_token_replaces_a_readable_file_with_an_owner_only_one(tmp_path):
    path = tmp_path / "me@acme.com.json"
    path.write_text('{"token": "old"}')
    path.chmod(0o644)  # e.g. written by an older version, or by hand
    save_token(path, '{"token": "new"}')
    assert path.read_text() == '{"token": "new"}'
    assert _mode(path) == 0o600


def test_save_token_never_exposes_the_token_while_writing(tmp_path, monkeypatch):
    """The temporary file is already 0600 when the token is written into it."""
    path = tmp_path / "me@acme.com.json"
    real_fdopen = os.fdopen
    modes = []

    def fdopen(fd, *args, **kwargs):
        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(gmail_module.os, "fdopen", fdopen)
    save_token(path, "{}")
    assert modes == [0o600]


def test_failed_save_keeps_the_previous_token_and_no_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "me@acme.com.json"
    path.write_text('{"token": "old"}')

    def replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(gmail_module.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        save_token(path, '{"token": "new"}')
    assert path.read_text() == '{"token": "old"}'
    assert _leftovers(tmp_path, path.name) == []


def test_save_token_tolerates_a_directory_that_cant_be_fsynced(tmp_path, monkeypatch):
    """Some filesystems refuse fsync on a directory (EINVAL): the token is saved anyway."""
    path = tmp_path / "me@acme.com.json"
    real_fsync = os.fsync
    refused = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            refused.append(fd)
            raise OSError(22, "Invalid argument")
        real_fsync(fd)

    monkeypatch.setattr(gmail_module.os, "fsync", fsync)
    save_token(path, '{"token": "new"}')
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
