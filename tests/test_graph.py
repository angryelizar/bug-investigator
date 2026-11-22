from bug_investigator.graph import build_graph
from bug_investigator.stages import (
    AgentGaveUp,
    Analysis,
    Critique,
    Fix,
    GitHubError,
    Workspace,
)
from bug_investigator.state import (
    Baseline,
    BuildResult,
    InvestigationState,
    RelatedIssue,
    Verdict,
)

BUG = {"bug_report": {"text": "Отмена брони падает с NPE", "source": "chat"}}

FROM_ISSUE = {
    "bug_report": {"text": "Отмена брони падает с NPE", "source": "issue", "issue_number": 7}
}

GREEN = Baseline(exit_code=0, log_tail="BUILD SUCCESS", failed_tests=[], exceptions=[])
RED = Baseline(
    exit_code=1,
    log_tail="BUILD FAILURE",
    failed_tests=["BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed"],
    exceptions=["ResourceException"],
)


def issue(number: int, title: str = "") -> RelatedIssue:
    return RelatedIssue(
        number=number,
        title=title or f"issue {number}",
        url=f"https://github.com/o/r/issues/{number}",
    )


class FakeStages:
    """Scripted stages: each critic/build call takes the next scripted answer."""

    def __init__(
        self,
        critiques=None,
        builds=None,
        verdict: Verdict = "confirmed",
        baseline: Baseline = GREEN,
        open_issues: list[RelatedIssue] | None = None,
        related_issue: int | None = None,
    ):
        self.related_issue = related_issue
        self.comments: list[tuple[int, str]] = []
        self.baseline_result = baseline
        self.open_issues = list(open_issues or [])
        self.searched_for: list[list[str]] = []
        self.analyst_saw: dict | None = None
        self.critiques = list(critiques or [Critique(passed=True, notes="")])
        self.builds = list(builds or [BuildResult(exit_code=0, log_tail="BUILD SUCCESS")])
        self.verdict: Verdict = verdict
        self.calls: list[str] = []
        self.planner_saw_notes: list[str | None] = []

    def prepare(self, state):
        self.calls.append("prepare")
        return Workspace(workdir="/tmp/wc", branch="bug-investigator/abc")

    def baseline(self, state):
        self.calls.append("baseline")
        return self.baseline_result

    def search_issues(self, terms):
        self.calls.append("search_issues")
        self.searched_for.append(terms)
        return self.open_issues

    def analyst(self, state):
        self.calls.append("analyst")
        self.analyst_saw = state
        return Analysis(
            verdict=self.verdict,
            summary="NPE в BookingService.cancel",
            related_issue=self.related_issue,
        )

    def comment_on_issue(self, number, body):
        self.calls.append("comment_on_issue")
        self.comments.append((number, body))

    def planner(self, state: InvestigationState):
        self.calls.append("planner")
        self.planner_saw_notes.append(state.get("critic_notes"))
        return "Проверить null в cancel"

    def coder(self, state):
        self.calls.append("coder")
        return Fix(diff="--- a\n+++ b\n", explanation="добавил проверку")

    def build(self, state):
        self.calls.append("build")
        return self.builds.pop(0) if len(self.builds) > 1 else self.builds[0]

    def critic(self, state):
        self.calls.append("critic")
        return self.critiques.pop(0) if len(self.critiques) > 1 else self.critiques[0]


def test_confirmed_bug_runs_all_stages_and_ends_with_fix_ready():
    stages = FakeStages()

    final = build_graph(stages).invoke(BUG)

    assert stages.calls == ["prepare", "baseline", "analyst", "planner", "coder", "build", "critic"]
    assert final["outcome"] == "fix_ready"
    assert final["workdir"] == "/tmp/wc"
    assert final["verdict"] == "confirmed"
    assert final["fix_diff"] == "--- a\n+++ b\n"
    assert final["build_result"]["exit_code"] == 0
    assert final["attempts"] == 1


def test_rejected_fix_goes_back_to_planner_with_critic_notes():
    stages = FakeStages(
        critiques=[
            Critique(passed=False, notes="diff не трогает cancel"),
            Critique(passed=True, notes=""),
        ]
    )

    final = build_graph(stages).invoke(BUG)

    assert stages.planner_saw_notes == [None, "diff не трогает cancel"]
    assert stages.calls.count("coder") == 2
    assert final["attempts"] == 2
    assert final["outcome"] == "fix_ready"


def test_agent_gives_up_after_three_rejected_attempts():
    stages = FakeStages(critiques=[Critique(passed=False, notes="всё ещё не то")])

    final = build_graph(stages).invoke(BUG)

    assert stages.calls.count("planner") == 3
    assert stages.calls.count("critic") == 3
    assert final["outcome"] == "gave_up"
    assert "всё ещё не то" in final["outcome_reason"]


def test_failed_build_sends_fix_back_even_if_critic_approves():
    stages = FakeStages(
        builds=[
            BuildResult(exit_code=1, log_tail="Tests run: 5, Failures: 1"),
            BuildResult(exit_code=0, log_tail="BUILD SUCCESS"),
        ]
    )

    final = build_graph(stages).invoke(BUG)

    assert stages.calls.count("planner") == 2
    assert "Failures: 1" in (stages.planner_saw_notes[1] or "")
    assert final["outcome"] == "fix_ready"


def test_unconfirmed_verdict_ends_investigation_without_planning():
    stages = FakeStages(verdict="insufficient_data")

    final = build_graph(stages).invoke(BUG)

    assert stages.calls == ["prepare", "baseline", "analyst"]
    assert final["outcome"] == "insufficient_data"
    assert final["outcome_reason"] == "NPE в BookingService.cancel"


def test_stage_that_gives_up_ends_investigation_as_gave_up():
    class CoderHitsToolLimit(FakeStages):
        def coder(self, state):
            self.calls.append("coder")
            raise AgentGaveUp("Coder исчерпал 15 вызовов инструментов")

    stages = CoderHitsToolLimit()

    final = build_graph(stages).invoke(BUG)

    assert stages.calls == ["prepare", "baseline", "analyst", "planner", "coder"]
    assert final["outcome"] == "gave_up"
    assert final["outcome_reason"] == "Coder исчерпал 15 вызовов инструментов"


def test_empty_fix_is_never_ready_even_if_critic_approves():
    class CoderChangesNothing(FakeStages):
        def coder(self, state):
            self.calls.append("coder")
            return Fix(diff="", explanation="ничего не менял")

    stages = CoderChangesNothing()

    final = build_graph(stages).invoke(BUG)

    assert stages.calls.count("planner") == 3
    assert final["outcome"] == "gave_up"
    assert "пустой" in final["outcome_reason"].lower()


def test_analyst_sees_the_baseline():
    red = Baseline(
        exit_code=1,
        log_tail="BUILD FAILURE",
        failed_tests=["BookingServiceTest.cancel"],
        exceptions=[],
    )
    stages = FakeStages(baseline=red)

    build_graph(stages).invoke(BUG)

    assert stages.analyst_saw is not None
    assert stages.analyst_saw["baseline"]["failed_tests"] == ["BookingServiceTest.cancel"]


def test_baseline_that_cannot_run_gives_up_before_any_model_stage():
    class DockerDown(FakeStages):
        def baseline(self, state):
            self.calls.append("baseline")
            raise AgentGaveUp("Docker-демон недоступен: Verification невозможна")

    stages = DockerDown()

    final = build_graph(stages).invoke(BUG)

    assert stages.calls == ["prepare", "baseline"]
    assert final["outcome"] == "gave_up"
    assert "Docker" in final["outcome_reason"]


def test_red_baseline_searches_open_issues_by_failed_tests_and_exceptions():
    stages = FakeStages(baseline=RED, open_issues=[issue(n) for n in range(1, 8)])

    build_graph(stages).invoke(BUG)

    [terms] = stages.searched_for
    assert "bookingRightAfterConfirmedOneIsAllowed" in terms
    assert "ResourceException" in terms
    assert stages.analyst_saw is not None
    assert [i["number"] for i in stages.analyst_saw["related_issues"]] == [1, 2, 3, 4, 5]


def test_green_baseline_does_not_search_issues():
    stages = FakeStages(baseline=GREEN)

    build_graph(stages).invoke(BUG)

    assert "search_issues" not in stages.calls


def test_failed_issue_search_does_not_stop_the_investigation():
    class GitHubDown(FakeStages):
        def search_issues(self, terms):
            raise GitHubError("gh: HTTP 503")

    stages = GitHubDown(baseline=RED)

    final = build_graph(stages).invoke(BUG)

    assert final["outcome"] == "fix_ready"
    assert stages.analyst_saw is not None
    assert stages.analyst_saw.get("related_issues", []) == []


def test_insufficient_data_comments_on_the_issue_with_what_is_missing():
    stages = FakeStages(verdict="insufficient_data")

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert final["outcome"] == "insufficient_data"
    [(number, body)] = stages.comments
    assert number == 7
    assert "Недостаточно данных" in body
    assert "NPE в BookingService.cancel" in body


def test_bug_report_from_chat_gets_no_issue_comment():
    stages = FakeStages(verdict="insufficient_data")

    build_graph(stages).invoke(BUG)

    assert stages.comments == []


def test_blocked_comments_with_failed_tests_and_the_related_issue():
    stages = FakeStages(
        verdict="blocked",
        baseline=RED,
        open_issues=[issue(1, "Нельзя забронировать слот сразу после чужой брони")],
        related_issue=1,
    )

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert stages.calls[-1] == "comment_on_issue"
    assert final["outcome"] == "blocked"
    assert final["related_issue"] == 1
    [(number, body)] = stages.comments
    assert number == 7
    assert "Заблокировано" in body
    assert "BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed" in body
    assert "#1" in body


def test_blocked_ignores_a_related_issue_that_the_search_did_not_find():
    stages = FakeStages(verdict="blocked", baseline=RED, open_issues=[], related_issue=42)

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert "related_issue" not in final
    [(_, body)] = stages.comments
    assert "#42" not in body


def test_not_a_bug_ends_without_a_comment_until_approval():
    stages = FakeStages(verdict="not_a_bug")

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert final["outcome"] == "not_a_bug"
    assert stages.comments == []
    assert "planner" not in stages.calls


def test_failed_issue_comment_keeps_the_verdict_and_says_so():
    class GitHubDown(FakeStages):
        def comment_on_issue(self, number, body):
            raise GitHubError("gh: HTTP 403")

    stages = GitHubDown(verdict="insufficient_data")

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert final["outcome"] == "insufficient_data"
    assert "HTTP 403" in final["outcome_reason"]


def test_blocked_verdict_on_a_green_baseline_is_not_trusted():
    stages = FakeStages(verdict="blocked", baseline=GREEN)

    final = build_graph(stages).invoke(FROM_ISSUE)

    assert final["outcome"] == "gave_up"
    assert stages.comments == []
