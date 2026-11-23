import subprocess
from pathlib import Path

import pytest

from bug_investigator import workspace


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A cloned-looking working copy: one commit with code and a test."""
    root = tmp_path / "wc"
    root.mkdir()
    git(root, "init", "--quiet")
    write(root, "Dockerfile", "FROM scratch\n")
    write(root, "src/main/Booking.java", "original code\n")
    write(root, "src/test/BookingTest.java", "original test\n")
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "init",
    )
    return root


def test_reproduction_copy_has_the_original_code_and_only_the_fixs_tests(repo: Path):
    write(repo, "src/main/Booking.java", "fixed code\n")
    write(repo, "src/test/BookingTest.java", "original test\nnew test method\n")
    write(repo, "src/test/CancelTest.java", "new test\n")
    write(repo, "module/src/test/ModuleTest.java", "nested module test\n")

    with workspace.reproduction_copy(str(repo)) as copy:
        assert copy is not None
        root = Path(copy)
        assert (root / "src/main/Booking.java").read_text() == "original code\n"
        assert (root / "src/test/BookingTest.java").read_text().endswith("new test method\n")
        assert (root / "src/test/CancelTest.java").read_text() == "new test\n"
        assert (root / "module/src/test/ModuleTest.java").exists()
        assert (root / "Dockerfile").exists()

    assert not root.exists()
    # The Fix itself stays in the working copy, ready for the Fix PR.
    assert (repo / "src/main/Booking.java").read_text() == "fixed code\n"
    assert (repo / "src/test/CancelTest.java").exists()
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_fix_without_tests_has_no_reproduction_copy(repo: Path):
    write(repo, "src/main/Booking.java", "fixed code\n")

    with workspace.reproduction_copy(str(repo)) as copy:
        assert copy is None


def test_deleted_test_is_not_a_reproduction(repo: Path):
    (repo / "src/test/BookingTest.java").unlink()

    with workspace.reproduction_copy(str(repo)) as copy:
        assert copy is None


def test_renamed_test_is_only_at_its_new_path_in_the_reproduction_copy(repo: Path):
    (repo / "src/test/BookingTest.java").rename(repo / "src/test/CancelTest.java")

    with workspace.reproduction_copy(str(repo)) as copy:
        assert copy is not None
        assert (Path(copy) / "src/test/CancelTest.java").exists()
        assert not (Path(copy) / "src/test/BookingTest.java").exists()
