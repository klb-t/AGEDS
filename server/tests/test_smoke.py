from pathlib import Path
from server.app.db import connect, SCHEMA

def test_schema_builds(tmp_path: Path):
    db=connect(tmp_path/'test.db')
    db.executescript(SCHEMA)
    assert db.execute("select name from sqlite_master where name='artifacts'").fetchone()
    assert db.execute("select name from sqlite_master where name='search_fts'").fetchone()
