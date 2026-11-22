import pytest

from bug_investigator.github import issue_number

REPO = "https://github.com/angryelizar/resourceBooking.git"


def test_issue_link_of_the_target_repository_gives_its_number():
    assert issue_number("https://github.com/angryelizar/resourceBooking/issues/12", REPO) == 12


def test_issue_link_tolerates_case_anchors_and_query():
    link = "https://github.com/AngryElizar/resourcebooking/issues/3#issuecomment-1?x=1"
    assert issue_number(link, REPO) == 3


@pytest.mark.parametrize(
    "link",
    [
        "https://github.com/someone/else/issues/12",  # another repository
        "https://github.com/angryelizar/resourceBooking/pull/12",  # a PR, not an issue
        "https://github.com/angryelizar/resourceBooking/issues",
        "https://github.com/angryelizar/resourceBooking-fork/issues/12",
        "12",
    ],
)
def test_anything_but_an_issue_of_the_target_repository_is_refused(link):
    with pytest.raises(ValueError):
        issue_number(link, REPO)
