import os
import stat

import pytest

from app.persistence import SQLiteStateStore


def test_existing_parent_permissions_are_not_changed(tmp_path):
    tmp_path.chmod(0o755)
    SQLiteStateStore(str(tmp_path / "demo.sqlite"))
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "demo.sqlite").stat().st_mode) == 0o600


def test_new_storage_directory_is_private(tmp_path):
    path = tmp_path / "new-private-directory" / "demo.sqlite"
    SQLiteStateStore(str(path))
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Requires POSIX no-follow support")
def test_symlink_storage_is_rejected_without_modifying_target(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("keep this content")
    target.chmod(0o644)
    path = tmp_path / "demo.sqlite"
    path.symlink_to(target)
    with pytest.raises(OSError):
        SQLiteStateStore(str(path))
    assert target.read_text() == "keep this content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_hardlinked_storage_is_rejected_without_modifying_target(tmp_path):
    target = tmp_path / "unrelated.txt"
    target.write_text("keep this content")
    target.chmod(0o644)
    path = tmp_path / "demo.sqlite"
    os.link(target, path)
    with pytest.raises(ValueError):
        SQLiteStateStore(str(path))
    assert target.read_text() == "keep this content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
