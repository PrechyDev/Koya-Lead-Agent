"""The least-privilege design, proven against the real database (specs.md §6, D-18)."""

import json
import uuid

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.db


def _insert_run(conn, key: str) -> str:
    return conn.execute(
        "insert into lead_agent.runs (idempotency_key, run_kind, objective, objective_hash, limits)"
        " values (%s, 'dev', 'permission test objective', 'x', %s) returning id",
        (key, json.dumps({"test": True})),
    ).fetchone()[0]


def test_app_role_can_insert_and_read_but_not_delete(app_dsn, admin_dsn):
    key = f"test-{uuid.uuid4()}"
    with psycopg.connect(app_dsn, prepare_threshold=None, autocommit=True) as conn:
        run_id = _insert_run(conn, key)
        row = conn.execute("select objective from lead_agent.runs where id = %s", (run_id,)).fetchone()
        assert row[0] == "permission test objective"
        conn.execute("update lead_agent.runs set status_detail = 'updated' where id = %s", (run_id,))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("delete from lead_agent.runs where id = %s", (run_id,))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("truncate lead_agent.tool_calls")
    # Clean-up needs the admin DSN, by design.
    with psycopg.connect(admin_dsn, prepare_threshold=None, autocommit=True) as conn:
        conn.execute("delete from lead_agent.runs where id = %s", (run_id,))


def test_supabase_public_roles_cannot_read_lead_agent(admin_dsn):
    with psycopg.connect(admin_dsn, prepare_threshold=None) as conn:
        for role in ("anon", "authenticated"):
            with conn.transaction(force_rollback=True):
                conn.execute(f"set local role {role}")
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute("select * from lead_agent.runs limit 1")


def test_last_admin_and_owner_are_protected(admin_dsn):
    """Runs in a rolled-back transaction with a throwaway auth user."""
    with psycopg.connect(admin_dsn, prepare_threshold=None) as conn:
        with conn.transaction(force_rollback=True):
            user_id = uuid.uuid4()
            conn.execute(
                "insert into auth.users (id, email, aud, role) values (%s, %s, 'authenticated', 'authenticated')",
                (user_id, f"test-{user_id}@example.invalid"),
            )
            others = conn.execute(
                "select count(*) from lead_agent.members where role = 'admin' and is_active"
            ).fetchone()[0]
            conn.execute(
                "insert into lead_agent.members (user_id, email, full_name, role, is_owner)"
                " values (%s, %s, 'Test Owner', 'admin', true)",
                (user_id, f"test-{user_id}@example.invalid"),
            )
            # The owner can't be demoted or deactivated.
            with pytest.raises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute("update lead_agent.members set role = 'member' where user_id = %s", (user_id,))
            with pytest.raises(errors.CheckViolation):
                with conn.transaction():
                    conn.execute("update lead_agent.members set is_active = false where user_id = %s", (user_id,))
            if others == 0:
                # Also the last active admin: un-own it, then try to demote.
                conn.execute("alter table lead_agent.members disable trigger members_protect_admins")
                conn.execute("update lead_agent.members set is_owner = false where user_id = %s", (user_id,))
                conn.execute("alter table lead_agent.members enable trigger members_protect_admins")
                with pytest.raises(errors.CheckViolation):
                    with conn.transaction():
                        conn.execute(
                            "update lead_agent.members set role = 'member' where user_id = %s", (user_id,)
                        )
