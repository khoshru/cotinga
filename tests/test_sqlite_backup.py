import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from deploy.ec2.backup_sqlite import backup_database


def test_backup_includes_uncheckpointed_wal_and_has_private_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE meals (description TEXT)")
        connection.execute("INSERT INTO meals VALUES (?)", ("Saved meal",))
        connection.commit()
        assert Path(str(source) + "-wal").exists()
        backup_database(source, destination)
        with closing(sqlite3.connect(destination)) as backup:
            assert backup.execute("SELECT description FROM meals").fetchone() == (
                "Saved meal",
            )
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert destination.stat().st_mode & 0o777 == 0o600


def test_backup_does_not_overwrite_an_existing_file(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)):
        pass
    destination = tmp_path / "backup.sqlite3"
    destination.write_text("Keep this existing backup")
    with pytest.raises(FileExistsError):
        backup_database(source, destination)
    assert destination.read_text() == "Keep this existing backup"


def test_missing_database_is_not_silently_created(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.sqlite3", tmp_path / "backup.sqlite3")
    assert list(tmp_path.iterdir()) == []


def test_invalid_database_does_not_leave_a_partial_backup(tmp_path: Path) -> None:
    source = tmp_path / "invalid.sqlite3"
    source.write_text("not a SQLite database")
    destination = tmp_path / "backup.sqlite3"
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, destination)
    assert not destination.exists()
