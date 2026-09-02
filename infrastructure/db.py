from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pymysql
from pymysql.connections import Connection

from ..config import Settings


class MySqlDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self) -> Connection:
        return pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_password,
            database=self.settings.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=20,
            read_timeout=180,
            write_timeout=180,
            autocommit=False,
        )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def apply_migrations(self, migrations_dir: Path) -> list[str]:
        with self.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ms_schema_migrations (
                        version varchar(128) NOT NULL,
                        applied_at timestamp NOT NULL DEFAULT current_timestamp(),
                        PRIMARY KEY (version)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """
                )
                cursor.execute("SELECT version FROM ms_schema_migrations")
                applied = {row["version"] for row in cursor.fetchall()}
                applied_now: list[str] = []
                for migration in sorted(migrations_dir.glob("*.sql")):
                    if migration.name in applied:
                        continue
                    sql = migration.read_text(encoding="utf-8")
                    for statement in _split_sql(sql):
                        cursor.execute(statement)
                    cursor.execute("INSERT INTO ms_schema_migrations (version) VALUES (%s)", (migration.name,))
                    applied_now.append(migration.name)
                return applied_now


def _split_sql(script: str) -> list[str]:
    statements = []
    for raw in script.split(";"):
        line_parts = [line for line in raw.splitlines() if not line.strip().startswith("--")]
        statement = "\n".join(line_parts).strip()
        if statement:
            statements.append(statement)
    return statements
