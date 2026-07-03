from datetime import datetime, timedelta, timezone

from src.logger import JsonlLogger


def test_write_creates_dated_file_and_appends_jsonl(tmp_path):
    logger = JsonlLogger(tmp_path, "decisions")
    ts = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    logger.write({"a": 1}, ts=ts)
    logger.write({"a": 2}, ts=ts)

    path = tmp_path / "decisions-2024-03-01.jsonl"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert '"a": 1' in lines[0]


def test_purge_removes_files_older_than_retention(tmp_path):
    old_date = datetime.now(timezone.utc) - timedelta(days=100)
    old_path = tmp_path / f"decisions-{old_date.strftime('%Y-%m-%d')}.jsonl"
    old_path.write_text('{"a": 1}\n')

    recent_date = datetime.now(timezone.utc) - timedelta(days=5)
    recent_path = tmp_path / f"decisions-{recent_date.strftime('%Y-%m-%d')}.jsonl"
    recent_path.write_text('{"a": 2}\n')

    JsonlLogger(tmp_path, "decisions")  # purge runs on construction

    assert not old_path.exists()
    assert recent_path.exists()
