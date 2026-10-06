"""Disposable real PostgreSQL server. Only a private temporary Unix socket is opened."""

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fl_scheduler.db.core import Database
from sqlalchemy import create_engine, text


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    executable = shutil.which("initdb")
    if executable is None:
        pytest.fail("PostgreSQL tools are required. Install the locked Pixi environment.")
    with tempfile.TemporaryDirectory(prefix="fl-pg-") as name:
        root = Path(name)
        data = root / "data"
        init = subprocess.run(
            [executable, "-D", str(data), "-A", "trust", "-U", "fl_test"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert init.returncode == 0, init.stderr
        control = str(Path(executable).with_name("pg_ctl"))
        start = subprocess.run(
            [
                control,
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-k {root} -p 6543 -c listen_addresses=''",
                "start",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert start.returncode == 0, start.stderr
        try:
            query = urlencode({"host": name, "port": 6543})
            yield f"postgresql+psycopg://fl_test@/postgres?{query}"
        finally:
            subprocess.run(
                [control, "-D", str(data), "stop", "-m", "fast", "-w"],
                capture_output=True,
                timeout=30,
                check=True,
            )


@pytest.fixture
def scheduler_db(postgres_url: str) -> Iterator[Database]:
    from uuid import uuid4

    schema = f"test_{uuid4().hex}"
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    query = urlencode({"options": f"-c search_path={schema}"})
    db = Database(postgres_url + "&" + query)
    db.create_test_schema()
    try:
        yield db
    finally:
        db.close()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()
