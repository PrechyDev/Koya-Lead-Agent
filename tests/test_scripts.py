"""scripts/create_app_role.py only reads .env and refuses a DSN that isn't the app user with a strong password."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("create_app_role", Path(__file__).parents[1] / "scripts" / "create_app_role.py")
car = importlib.util.module_from_spec(spec)
spec.loader.exec_module(car)
ADMIN = "postgresql://postgres.abcref:adminpw@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"


def test_template_has_no_secret_and_uses_the_app_user():
    t = car.dsn_template(ADMIN)
    assert t == "postgresql://lead_agent_app.abcref:<your-password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
    assert "adminpw" not in t


@pytest.mark.parametrize("dsn", [
    "postgresql://postgres.abcref:" + "x" * 30 + "@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",  # admin user
    "postgresql://lead_agent_app.abcref:short@aws-0-eu-west-1.pooler.supabase.com:5432/postgres",       # weak password
    "postgresql://lead_agent_app.abcref:" + "x" * 30 + "@other-host.example.com:5432/postgres",         # wrong host
])
def test_bad_app_dsn_is_refused(dsn):
    with pytest.raises(SystemExit):
        car.app_credentials(dsn, ADMIN)


def test_script_never_writes_env():
    source = (Path(__file__).parents[1] / "scripts" / "create_app_role.py").read_text(encoding="utf-8")
    assert "write_text" not in source and "os.replace" not in source and 'open(' not in source
