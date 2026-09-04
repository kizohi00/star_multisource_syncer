from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pymysql
from pymysql.connections import Connection

from ..config import Settings


class MySqlDatabase:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self) -> Connection:
        connection = pymysql.connect(
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
        sync_database = self.settings.effective_sync_db_name
        if sync_database == self.settings.db_name:
            return connection
        return QualifiedConnection(connection, sync_database)

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


class QualifiedConnection:
    """Route every ``ms_*`` table reference to the sync database.

    The connection remains selected on the content database, so unqualified
    ``series``, ``chapters`` and ``pages`` continue to target Manga Star while
    the sync tables are transparently addressed in the second schema. This
    also keeps the canonical write and its sync bookkeeping in one transaction.
    """

    def __init__(self, connection: Connection, sync_database: str) -> None:
        self._connection = connection
        self._sync_database = _quote_identifier(sync_database)

    def cursor(self, *args: Any, **kwargs: Any) -> "QualifiedCursor":
        return QualifiedCursor(
            self._connection.cursor(*args, **kwargs),
            self._sync_database,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class QualifiedCursor:
    def __init__(self, cursor: Any, sync_database: str) -> None:
        self._cursor = cursor
        self._sync_database = sync_database

    def execute(self, query: str, args: Any = None) -> Any:
        return self._cursor.execute(
            qualify_sync_tables(query, self._sync_database),
            args,
        )

    def executemany(self, query: str, args: Any) -> Any:
        return self._cursor.executemany(
            qualify_sync_tables(query, self._sync_database),
            args,
        )

    def __enter__(self) -> "QualifiedCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


def qualify_sync_tables(query: str, sync_database: str) -> str:
    """Qualify bare ``ms_*`` identifiers without changing quoted values."""
    result: list[str] = []
    index = 0
    length = len(query)
    while index < length:
        character = query[index]
        if character in ("'", '"'):
            quote = character
            end = index + 1
            while end < length:
                if query[end] == "\\":
                    end += 2
                    continue
                if query[end] == quote:
                    end += 1
                    break
                end += 1
            result.append(query[index:end])
            index = end
            continue
        if character == "`":
            end = query.find("`", index + 1)
            if end == -1:
                result.append(query[index:])
                break
            identifier = query[index + 1:end]
            previous = query[index - 1] if index else ""
            if identifier.startswith("ms_") and previous != ".":
                result.append(f"{sync_database}.`{identifier}`")
            else:
                result.append(query[index:end + 1])
            index = end + 1
            continue
        if (character.isalpha() or character == "_") and (
            index == 0 or not (query[index - 1].isalnum() or query[index - 1] == "_")
        ):
            end = index + 1
            while end < length and (query[end].isalnum() or query[end] == "_"):
                end += 1
            identifier = query[index:end]
            next_character = query[end] if end < length else ""
            previous = query[index - 1] if index else ""
            if (
                identifier.startswith("ms_")
                and previous != "."
                and not (next_character.isalnum() or next_character == "_")
            ):
                result.append(f"{sync_database}.`{identifier}`")
            else:
                result.append(identifier)
            index = end
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _quote_identifier(value: str) -> str:
    if not value or "`" in value:
        raise ValueError("Database identifiers cannot be empty or contain backticks.")
    return f"`{value}`"


def _split_sql(script: str) -> list[str]:
    statements = []
    for raw in script.split(";"):
        line_parts = [line for line in raw.splitlines() if not line.strip().startswith("--")]
        statement = "\n".join(line_parts).strip()
        if statement:
            statements.append(statement)
    return statements
