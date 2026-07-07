"""Regression tests for the auth-store wipe guard (_save_auth_store).

Root cause of the recurring nous (and all-provider) auth deaths: an
unreadable/corrupt auth.json read makes _load_auth_store return an EMPTY
store, and the next read-modify-write (esp. write_credential_pool) persists
that empty store, silently deleting every credential with no last_auth_error.
The guard refuses to overwrite a populated store with an empty one unless an
intentional clear passes allow_provider_shrink=True.
"""

import json
import io
import pytest


@pytest.fixture()
def auth_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Some builds cache the resolved home; import fresh + point the module's
    # path helper at the tmp store to be safe.
    import hermes_cli.auth as auth
    monkeypatch.setattr(auth, "_auth_file_path", lambda: tmp_path / "auth.json")
    return tmp_path, auth


def _write(path, obj):
    io.open(path, "w", encoding="utf-8").write(json.dumps(obj))


class TestWipeGuard:
    def test_refuses_empty_over_populated(self, auth_home):
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {"nous": {"access_token": "x"}}})
        with pytest.raises(auth.AuthStoreWipeGuard):
            auth._save_auth_store({"version": 1, "providers": {}})
        # disk store MUST be untouched — nous still present
        after = json.load(io.open(p, encoding="utf-8"))
        assert "nous" in after["providers"], "populated store was wiped despite guard"

    def test_refuses_empty_over_pool_only(self, auth_home):
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {}, "credential_pool": {"anthropic": [{"id": "a"}]}})
        with pytest.raises(auth.AuthStoreWipeGuard):
            auth._save_auth_store({"version": 1, "providers": {}, "credential_pool": {}})
        after = json.load(io.open(p, encoding="utf-8"))
        assert after.get("credential_pool"), "pool wiped despite guard"

    def test_refuses_empty_when_existing_unreadable(self, auth_home):
        # Simulate the exact incident: existing file present but unparseable
        # (stands in for Errno 13 / partial read). New store empty, no force.
        tmp, auth = auth_home
        p = tmp / "auth.json"
        io.open(p, "w", encoding="utf-8").write("{ this is not json")
        with pytest.raises(auth.AuthStoreWipeGuard):
            auth._save_auth_store({"version": 1, "providers": {}})

    def test_refuses_empty_over_nondict_json(self, auth_home):
        # Parseable but non-dict shape (null / [] / "str") is unknown -> fail safe.
        tmp, auth = auth_home
        p = tmp / "auth.json"
        io.open(p, "w", encoding="utf-8").write("null")
        with pytest.raises(auth.AuthStoreWipeGuard):
            auth._save_auth_store({"version": 1, "providers": {}})

    def test_allows_shrink_when_forced(self, auth_home):
        # Intentional logout / provider removal.
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {"nous": {"access_token": "x"}}})
        auth._save_auth_store({"version": 1, "providers": {}}, allow_provider_shrink=True)
        after = json.load(io.open(p, encoding="utf-8"))
        assert after["providers"] == {}, "forced clear should empty the store"

    def test_allows_empty_over_empty(self, auth_home):
        # No data loss possible — should not raise.
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {}})
        auth._save_auth_store({"version": 1, "providers": {}})
        assert (tmp / "auth.json").exists()

    def test_allows_normal_populated_write(self, auth_home):
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {"nous": {"access_token": "old"}}})
        auth._save_auth_store({"version": 1, "providers": {"nous": {"access_token": "new"}}})
        after = json.load(io.open(p, encoding="utf-8"))
        assert after["providers"]["nous"]["access_token"] == "new"

    def test_allows_first_write_when_absent(self, auth_home):
        # No existing file -> genuinely first run -> empty is fine.
        tmp, auth = auth_home
        auth._save_auth_store({"version": 1, "providers": {}})
        assert (tmp / "auth.json").exists()

    def test_clear_provider_auth_of_last_provider_succeeds(self, auth_home):
        # End-to-end: clearing the last provider is intentional and must work
        # (it forces the shrink internally).
        tmp, auth = auth_home
        p = tmp / "auth.json"
        _write(p, {"version": 1, "providers": {"nous": {"access_token": "x"}}, "active_provider": "nous"})
        assert auth.clear_provider_auth("nous") is True
        after = json.load(io.open(p, encoding="utf-8"))
        assert "nous" not in after.get("providers", {})
