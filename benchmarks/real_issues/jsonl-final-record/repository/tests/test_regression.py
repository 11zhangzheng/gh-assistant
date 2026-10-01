from records import read_records


def test_last_record_without_newline(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text('{"id": 1}\n{"id": 2}', encoding="utf-8")
    assert read_records(source) == [{"id": 1}, {"id": 2}]


def test_blank_lines_are_ignored(tmp_path):
    source = tmp_path / "events.jsonl"
    source.write_text('\n{"id": 1}\n\n', encoding="utf-8")
    assert read_records(source) == [{"id": 1}]
