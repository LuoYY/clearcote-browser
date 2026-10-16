"""Offline mode: an Offline Key file replaces every licence call (no checkout, heartbeat, check-in or
download); its token is handed to the engine as CLEARCOTE_RUN_TOKEN and the binary must be given."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import clearcote
from clearcote import _offline
from clearcote._license import inject_run_token

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def write_key(tmp_path, **over):
    data = {
        "format": "clearcote-offline-key",
        "v": 1,
        "licence_id": "lic_1",
        "expires_at": (NOW + timedelta(days=40)).isoformat().replace("+00:00", "Z"),
        "token": "eyJwYXlsb2FkIjoxfQ.c2ln",
    }
    data.update(over)
    p = tmp_path / "key.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_no_key_means_online_mode(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_OFFLINE_KEY", raising=False)
    assert _offline.offline_lease(None) is None


def test_a_valid_key_gives_its_token_and_no_network(tmp_path):
    lease = _offline.offline_lease(write_key(tmp_path), now=NOW)
    assert lease.token == "eyJwYXlsb2FkIjoxfQ.c2ln"
    assert lease.bind_launch()[0] is None
    assert lease.refresh_token is None
    lease.stop()  # no-op


def test_env_var_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_OFFLINE_KEY", write_key(tmp_path))
    assert _offline.offline_key_path() is not None


@pytest.mark.parametrize(
    "over,msg",
    [
        ({"expires_at": (NOW - timedelta(days=1)).isoformat()}, "expired"),
        ({"format": "something-else"}, "not a Clearcote offline key"),
        ({"token": "no-dot"}, "no valid token"),
        ({"expires_at": "soon"}, "no valid expires_at"),
    ],
)
def test_bad_keys_fail_clearly(tmp_path, over, msg):
    with pytest.raises(clearcote.OfflineKeyError, match=msg):
        _offline.offline_lease(write_key(tmp_path, **over), now=NOW)


def test_missing_and_unreadable_files(tmp_path):
    with pytest.raises(clearcote.OfflineKeyError, match="not found"):
        _offline.offline_lease(str(tmp_path / "nope.json"), now=NOW)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(clearcote.OfflineKeyError, match="JSON"):
        _offline.offline_lease(str(bad), now=NOW)


def test_warns_when_close_to_expiry(tmp_path, capsys):
    _offline.offline_lease(write_key(tmp_path, expires_at=(NOW + timedelta(days=3)).isoformat()), now=NOW)
    assert "expires in 3 day(s)" in capsys.readouterr().err
    _offline.offline_lease(write_key(tmp_path, expires_at=(NOW + timedelta(days=3)).isoformat()), quiet=True, now=NOW)
    assert capsys.readouterr().err == ""


def test_launch_path_takes_no_lease_and_needs_a_binary(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("offline mode must not contact the licence server")

    monkeypatch.setattr(clearcote, "acquire_lease", boom)
    monkeypatch.delenv("CLEARCOTE_BINARY", raising=False)
    key = write_key(tmp_path, expires_at=(datetime.now(timezone.utc) + timedelta(days=40)).isoformat())

    kwargs = {"offline_key": key, "license_key": "cc_lic_x", "license_through_proxy": True}
    with pytest.raises(clearcote.OfflineKeyError, match="executable_path"):
        clearcote._acquire_lease_from_kwargs(kwargs)

    kwargs = {"offline_key": key, "license_key": "cc_lic_x", "executable_path": "/opt/clearcote/chrome"}
    lease = clearcote._acquire_lease_from_kwargs(kwargs)
    assert lease.token == "eyJwYXlsb2FkIjoxfQ.c2ln"
    assert "offline_key" not in kwargs and "license_key" not in kwargs
    assert kwargs["_cc_pro"] is None  # never selects/downloads the PRO build
    assert kwargs["_cc_lease"] is lease

    pw = {}
    inject_run_token(pw, lease.token, lease.bind_launch()[0])
    assert pw["env"]["CLEARCOTE_RUN_TOKEN"] == "eyJwYXlsb2FkIjoxfQ.c2ln"
    assert "CLEARCOTE_RUN_TOKEN_FILE" not in pw["env"]


def test_binary_from_env_is_enough(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_BINARY", "/opt/clearcote/chrome")
    key = write_key(tmp_path, expires_at=(datetime.now(timezone.utc) + timedelta(days=40)).isoformat())
    lease = clearcote._acquire_lease_from_kwargs({"offline_key": key})
    assert lease.token
