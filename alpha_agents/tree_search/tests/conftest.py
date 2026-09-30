import pytest


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "harness"
    path.mkdir()
    (path / "value.txt").write_text("0")
    return path
