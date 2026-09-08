"""Unit tests for backend.auth: password hashing, org/user creation, and
session lookup. Pure filesystem logic, no HTTP layer — the endpoint-level
login/logout/cross-org-isolation behavior is covered in
tests/test_backend_api.py instead."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth


def test_hash_and_verify_password_round_trip():
    stored = auth.hash_password("correct horse battery staple")
    assert auth.verify_password("correct horse battery staple", stored)
    assert not auth.verify_password("wrong password", stored)


def test_hash_password_uses_a_fresh_salt_each_time():
    a = auth.hash_password("same password")
    b = auth.hash_password("same password")
    assert a != b  # same password, different salt -> different stored value
    assert auth.verify_password("same password", a)
    assert auth.verify_password("same password", b)


def test_verify_password_rejects_malformed_stored_value():
    assert not auth.verify_password("anything", "not-a-real-hash")


def test_create_org_and_list_orgs(tmp_path):
    org = auth.create_org(tmp_path, "Grace Community Church")
    orgs = auth.list_orgs(tmp_path)
    assert org["org_id"] in orgs
    assert orgs[org["org_id"]]["name"] == "Grace Community Church"


def test_create_user_requires_existing_org(tmp_path):
    try:
        auth.create_user(tmp_path, "no-such-org", "alice", "pw")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no such org" in str(e)


def test_create_user_rejects_duplicate_username(tmp_path):
    org = auth.create_org(tmp_path, "Org")
    auth.create_user(tmp_path, org["org_id"], "alice", "pw1")
    try:
        auth.create_user(tmp_path, org["org_id"], "alice", "pw2")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "already exists" in str(e)


def test_authenticate_succeeds_with_correct_password(tmp_path):
    org = auth.create_org(tmp_path, "Org")
    auth.create_user(tmp_path, org["org_id"], "alice", "correct-pw")
    user = auth.authenticate(tmp_path, "alice", "correct-pw")
    assert user is not None
    assert user["username"] == "alice"
    assert user["org_id"] == org["org_id"]


def test_authenticate_fails_with_wrong_password(tmp_path):
    org = auth.create_org(tmp_path, "Org")
    auth.create_user(tmp_path, org["org_id"], "alice", "correct-pw")
    assert auth.authenticate(tmp_path, "alice", "wrong-pw") is None


def test_authenticate_fails_for_unknown_username(tmp_path):
    assert auth.authenticate(tmp_path, "nobody", "pw") is None


def test_two_users_same_org_share_org_id(tmp_path):
    # the real point of the org-as-boundary design: two accounts at the
    # same church resolve to the same org_id, so they see the same batches
    org = auth.create_org(tmp_path, "Org")
    alice = auth.create_user(tmp_path, org["org_id"], "alice", "pw")
    bob = auth.create_user(tmp_path, org["org_id"], "bob", "pw")
    assert alice["org_id"] == bob["org_id"] == org["org_id"]
    assert alice["user_id"] != bob["user_id"]


def test_create_and_resolve_session(tmp_path):
    org = auth.create_org(tmp_path, "Org")
    user = auth.create_user(tmp_path, org["org_id"], "alice", "pw")
    token = auth.create_session(tmp_path, user["user_id"], user["org_id"])
    session = auth.resolve_session(tmp_path, token)
    assert session == {"user_id": user["user_id"], "org_id": user["org_id"]}


def test_resolve_session_none_for_missing_token(tmp_path):
    assert auth.resolve_session(tmp_path, "not-a-real-token") is None


def test_resolve_session_none_for_none_token(tmp_path):
    assert auth.resolve_session(tmp_path, None) is None


def test_resolve_session_none_for_expired_token(tmp_path, monkeypatch):
    org = auth.create_org(tmp_path, "Org")
    user = auth.create_user(tmp_path, org["org_id"], "alice", "pw")
    token = auth.create_session(tmp_path, user["user_id"], user["org_id"])

    # move the session's own expiry into the past directly, rather than
    # waiting out a real 30-day TTL
    sessions = auth._load_json(auth._sessions_path(tmp_path))
    sessions[token]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    auth._save_json(auth._sessions_path(tmp_path), sessions)

    assert auth.resolve_session(tmp_path, token) is None


def test_delete_session_invalidates_it(tmp_path):
    org = auth.create_org(tmp_path, "Org")
    user = auth.create_user(tmp_path, org["org_id"], "alice", "pw")
    token = auth.create_session(tmp_path, user["user_id"], user["org_id"])
    assert auth.resolve_session(tmp_path, token) is not None

    auth.delete_session(tmp_path, token)
    assert auth.resolve_session(tmp_path, token) is None


def test_delete_session_missing_token_is_a_noop(tmp_path):
    auth.delete_session(tmp_path, "never-existed")  # must not raise
    auth.delete_session(tmp_path, None)  # must not raise
