from concurrent.futures import ThreadPoolExecutor

from pico.persistence import write_once_bytes


def test_concurrent_publishers_do_not_overwrite_each_other(tmp_path):
    target = tmp_path / "artifact.txt"
    payloads = [b"first" * 1000, b"second" * 1000]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda data: write_once_bytes(target, data), payloads))
    assert sum(results) == 1
    assert target.read_bytes() == payloads[results.index(True)]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["artifact.txt"]


def test_existing_artifact_content_and_mode_are_preserved(tmp_path):
    target = tmp_path / "artifact.txt"
    assert write_once_bytes(target, b"original", mode=0o640)
    assert target.stat().st_mode & 0o777 == 0o640
    assert not write_once_bytes(target, b"replacement", mode=0o600)
    assert target.read_bytes() == b"original"
    assert target.stat().st_mode & 0o777 == 0o640
