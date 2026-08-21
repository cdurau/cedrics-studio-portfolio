import os
import shutil
import subprocess
import pytest
import sqlite3

@pytest.fixture
def isolated_alembic(tmp_path):
    base_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    shutil.copy(os.path.join(base_dir, "alembic.ini"), tmp_path / "alembic.ini")
    shutil.copytree(os.path.join(base_dir, "alembic"), tmp_path / "alembic")
    shutil.copytree(os.path.join(base_dir, "source"), tmp_path / "source")
    return tmp_path

def test_migration_upgrade_head(isolated_alembic):
    db_path = isolated_alembic / "test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(isolated_alembic)

    res = subprocess.run(["alembic", "upgrade", "head"], cwd=isolated_alembic, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entries'")
    assert cur.fetchone() is not None
    conn.close()

def test_migration_fail_closed(isolated_alembic):
    db_path = isolated_alembic / "test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(isolated_alembic)

    subprocess.run(["alembic", "upgrade", "head"], cwd=isolated_alembic, env=env, check=True)

    bad_revision_code = """
from alembic import op
import sqlalchemy as sa

revision = '0002_fail'
down_revision = '0001'
branch_labels = None
depends_on = None

def upgrade():
    raise Exception("Intentional failure")
    op.create_table('should_not_exist', sa.Column('id', sa.Integer(), primary_key=True))

def downgrade():
    pass
"""
    bad_rev_path = isolated_alembic / "alembic" / "versions" / "0002_fail.py"
    bad_rev_path.write_text(bad_revision_code)

    res = subprocess.run(["alembic", "upgrade", "head"], cwd=isolated_alembic, env=env, capture_output=True, text=True)
    assert res.returncode != 0

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='should_not_exist'")
    assert cur.fetchone() is None

    cur.execute("SELECT version_num FROM alembic_version")
    version = cur.fetchone()[0]
    assert version == "0001"
    conn.close()
