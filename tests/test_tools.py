from pathlib import Path

import pytest

from bug_investigator.tools import build_tools

BOOKING = """package app;

public class BookingService {
    public void cancel(Booking booking) {
        booking.getResource().release();
    }
}
"""

CONTROLLER = """package app;

public class BookingController {
    void cancel(Booking booking) {
        bookingService.cancel(booking);
    }
}
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    src = tmp_path / "src/main/java/app"
    src.mkdir(parents=True)
    (src / "BookingService.java").write_text(BOOKING)
    (src / "BookingController.java").write_text(CONTROLLER)
    (tmp_path / "README.md").write_text("# resourceBooking\n")
    return tmp_path


def call(tools, name: str, **args) -> str:
    by_name = {t.name: t for t in tools}
    return by_name[name].invoke(args)


def test_grep_reports_matching_lines_with_path_and_line_number(workdir):
    tools = build_tools(workdir, can_write=False)

    out = call(tools, "grep", pattern="getResource")

    assert "src/main/java/app/BookingService.java:5:" in out
    assert "booking.getResource().release();" in out


def test_grep_without_matches_says_so_in_text(workdir):
    tools = build_tools(workdir, can_write=False)

    out = call(tools, "grep", pattern="noSuchSymbol")

    assert "noSuchSymbol" in out
    assert "ничего не найдено" in out.lower()


def test_grep_definitions_only_skips_call_sites(workdir):
    tools = build_tools(workdir, can_write=False)

    out = call(tools, "grep", pattern="cancel", definitions_only=True)

    assert "BookingService.java:4:" in out
    assert "BookingController.java:4:" in out
    assert "BookingController.java:5:" not in out


def test_find_files_matches_by_name_fragment_and_by_glob(workdir):
    tools = build_tools(workdir, can_write=False)

    by_name = call(tools, "find_files", query="BookingServ")
    by_glob = call(tools, "find_files", query="**/*.md")

    assert by_name.splitlines()[0] == "src/main/java/app/BookingService.java"
    assert by_glob.splitlines() == ["README.md"]


def test_read_only_agents_can_list_and_read_but_not_write(workdir):
    tools = build_tools(workdir, can_write=False)

    assert {t.name for t in tools} == {"grep", "find_files", "list_directory", "read_file"}
    assert "BookingService.java" in call(tools, "list_directory", dir_path="src/main/java/app")
    assert call(tools, "read_file", file_path="README.md") == "# resourceBooking\n"


def test_coder_writes_files_and_search_sees_them(workdir):
    tools = build_tools(workdir, can_write=True)

    call(
        tools,
        "write_file",
        file_path="src/test/java/app/CancelTest.java",
        text="class CancelTest { void cancelReleasesResource() {} }\n",
    )

    assert (workdir / "src/test/java/app/CancelTest.java").exists()
    assert "CancelTest.java:1:" in call(tools, "grep", pattern="cancelReleasesResource")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"file_path": "Big.java"}),
        ("grep", {"pattern": "repeated"}),
    ],
)
def test_long_output_is_cut_to_the_limit_with_a_notice(workdir, tool, args):
    (workdir / "Big.java").write_text("int repeated = 1;\n" * 2_000)
    tools = build_tools(workdir, can_write=False, max_output_chars=1_000)

    out = call(tools, tool, **args)

    assert len(out) <= 1_000
    assert out.startswith("int repeated") or out.startswith("Big.java:1:")
    assert "обрезан" in out


def test_files_outside_the_working_copy_are_refused(workdir):
    (workdir.parent / "secret.txt").write_text("token")
    tools = build_tools(workdir, can_write=True)

    read = call(tools, "read_file", file_path="../secret.txt")
    write = call(tools, "write_file", file_path="../evil.txt", text="x")

    assert "token" not in read
    assert "Access denied" in read and "Access denied" in write
    assert not (workdir.parent / "evil.txt").exists()
