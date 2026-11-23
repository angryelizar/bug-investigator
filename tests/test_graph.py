import itertools

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from bug_investigator.graph import build_graph
from bug_investigator.stages import (
    AgentGaveUp,
    Analysis,
    Critique,
    Fix,
    GitHubError,
    PublishError,
    Workspace,
)
from bug_investigator.state import (
    Baseline,
    BuildResult,
    InvestigationState,
    RelatedIssue,
    ReproductionBuild,
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


APPROVE = {"decision": "approve", "comment": ""}
REJECT = {"decision": "reject", "comment": ""}


def revise(comment: str) -> dict:
    return {"decision": "revise", "comment": comment}


_threads = itertools.count()


def run(stages, initial, answers=None, **graph_options):
    """Run an Investigation to its end, answering each pause in turn; with no answers
    every pause is approved. Returns the final state and the kinds of pauses seen."""
    graph = build_graph(stages, **graph_options)
    config: RunnableConfig = {"configurable": {"thread_id": str(next(_threads))}}
    pending = None if answers is None else list(answers)
    pauses: list[str] = []
    graph.invoke(initial, config)
    while interrupts := graph.get_state(config).interrupts:
        pauses.append(interrupts[0].value["kind"])
        answer = APPROVE if pending is None else pending.pop(0)
        graph.invoke(Command(resume=answer), config)
    assert not pending, f"unused answers: {pending}"
    return graph.get_state(config).values, pauses


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
        recent_issues: list[RelatedIssue] | None = None,
        reproduction: ReproductionBuild | None = None,
    ):
        self.reproduction = reproduction
        self.recent_issues = list(recent_issues or [])
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
        self.planner_saw: list[dict] = []
        self.published: list[tuple[str, str]] = []

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

    def recent_open_issues(self, limit):
        self.calls.append("recent_open_issues")
        return self.recent_issues[:limit]

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
        self.planner_saw.append(dict(state))
        return "Проверить null в cancel"

    def coder(self, state):
        self.calls.append("coder")
        return Fix(diff="--- a\n+++ b\n", explanation="добавил проверку")

    def build(self, state):
        self.calls.append("build")
        return self.builds.pop(0) if len(self.builds) > 1 else self.builds[0]

    def build_reproduction(self, state):
        self.calls.append("build_reproduction")
        return self.reproduction

    def critic(self, state):
        self.calls.append("critic")
        return self.critiques.pop(0) if len(self.critiques) > 1 else self.critiques[0]

    def publish(self, state, title, body):
        self.calls.append("publish")
        self.published.append((title, body))
        return "https://github.com/o/r/pull/9"

    def cleanup(self, state):
        self.calls.append("cleanup")


def test_confirmed_bug_pauses_on_plan_and_fix_then_opens_a_fix_pr():
    stages = FakeStages()

    final, pauses = run(stages, BUG)

    assert pauses == ["plan", "fix"]
    assert stages.calls == [
        "prepare",
        "baseline",
        "analyst",
        "planner",
        "coder",
        "build",
        "build_reproduction",
        "critic",
        "publish",
        "cleanup",
    ]
    assert final["outcome"] == "pr_created"
    assert final["pr_url"] == "https://github.com/o/r/pull/9"
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

    final, _ = run(stages, BUG)

    assert stages.planner_saw_notes == [None, "diff не трогает cancel"]
    assert stages.calls.count("coder") == 2
    assert final["attempts"] == 2
    assert final["outcome"] == "pr_created"


def test_agent_gives_up_after_three_rejected_attempts():
    stages = FakeStages(critiques=[Critique(passed=False, notes="всё ещё не то")])

    final, _ = run(stages, BUG)

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

    final, _ = run(stages, BUG)

    assert stages.calls.count("planner") == 2
    assert "Failures: 1" in (stages.planner_saw_notes[1] or "")
    assert final["outcome"] == "pr_created"


RED_REPRODUCTION = ReproductionBuild(
    exit_code=1,
    log_tail="Tests run: 1, Failures: 1",
    failed_tests=["BookingServiceTest.cancelWithoutPayment"],
)


def test_fix_whose_tests_fail_on_the_original_code_is_reproduced():
    stages = FakeStages(reproduction=RED_REPRODUCTION)

    final, _ = run(stages, BUG)

    assert final["reproduced"] is True
    assert final["reproduction_build"] == RED_REPRODUCTION


def test_reproduction_that_failed_before_any_test_ran_proves_nothing():
    # E.g. the test uses API that only the Fix adds: build A fails on compilation.
    stages = FakeStages(
        reproduction=ReproductionBuild(exit_code=1, log_tail="COMPILATION ERROR", failed_tests=[])
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False


def test_tests_already_red_in_the_baseline_do_not_reproduce_the_bug():
    stages = FakeStages(
        baseline=RED,
        reproduction=ReproductionBuild(
            exit_code=1, log_tail="BUILD FAILURE", failed_tests=RED["failed_tests"]
        ),
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False


def test_new_failure_on_top_of_a_red_baseline_reproduces_the_bug():
    stages = FakeStages(
        baseline=RED,
        reproduction=ReproductionBuild(
            exit_code=1,
            log_tail="BUILD FAILURE",
            failed_tests=[*RED["failed_tests"], "BookingServiceTest.cancelWithoutPayment"],
        ),
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is True


def test_fix_without_tests_is_unreproduced():
    stages = FakeStages(reproduction=None)

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False
    assert final["reproduction_build"] is None


def test_fix_whose_tests_pass_on_the_original_code_is_unreproduced():
    stages = FakeStages(
        reproduction=ReproductionBuild(exit_code=0, log_tail="BUILD SUCCESS", failed_tests=[])
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False


def test_reproduction_that_timed_out_on_the_original_code_proves_nothing():
    stages = FakeStages(
        reproduction=ReproductionBuild(
            exit_code=124, log_tail="таймаут", failed_tests=["BookingServiceTest.slow"]
        )
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False


def test_failed_fix_build_skips_the_reproduction_build():
    stages = FakeStages(
        builds=[
            BuildResult(exit_code=1, log_tail="Tests run: 5, Failures: 1"),
            BuildResult(exit_code=0, log_tail="BUILD SUCCESS"),
        ],
        reproduction=RED_REPRODUCTION,
    )

    final, _ = run(stages, BUG)

    assert stages.calls.count("build") == 2
    assert stages.calls.count("build_reproduction") == 1
    assert final["reproduced"] is True


def test_rejected_reproduced_fix_does_not_keep_its_reproduction_for_the_next_one():
    class ReproducedOnlyFirst(FakeStages):
        def build_reproduction(self, state):
            super().build_reproduction(state)
            first = self.calls.count("build_reproduction") == 1
            return RED_REPRODUCTION if first else None

    stages = ReproducedOnlyFirst(
        critiques=[Critique(passed=False, notes="не то"), Critique(passed=True, notes="")]
    )

    final, _ = run(stages, BUG)

    assert final["reproduced"] is False


def test_critic_sees_whether_the_fix_is_reproduced():
    seen: list[dict] = []

    class Watching(FakeStages):
        def critic(self, state):
            seen.append(dict(state))
            return super().critic(state)

    run(Watching(reproduction=RED_REPRODUCTION), BUG)

    assert seen[0]["reproduced"] is True


def test_unconfirmed_verdict_ends_investigation_without_planning():
    stages = FakeStages(verdict="insufficient_data")

    final, _ = run(stages, BUG)

    assert stages.calls == ["prepare", "baseline", "analyst", "cleanup"]
    assert final["outcome"] == "insufficient_data"
    assert final["outcome_reason"] == "NPE в BookingService.cancel"


def test_stage_that_gives_up_ends_investigation_as_gave_up():
    class CoderHitsToolLimit(FakeStages):
        def coder(self, state):
            self.calls.append("coder")
            raise AgentGaveUp("Coder исчерпал 15 вызовов инструментов")

    stages = CoderHitsToolLimit()

    final, _ = run(stages, BUG)

    assert stages.calls == ["prepare", "baseline", "analyst", "planner", "coder", "cleanup"]
    assert final["outcome"] == "gave_up"
    assert final["outcome_reason"] == "Coder исчерпал 15 вызовов инструментов"


def test_empty_fix_is_never_ready_even_if_critic_approves():
    class CoderChangesNothing(FakeStages):
        def coder(self, state):
            self.calls.append("coder")
            return Fix(diff="", explanation="ничего не менял")

    stages = CoderChangesNothing()

    final, _ = run(stages, BUG)

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

    run(stages, BUG)

    assert stages.analyst_saw is not None
    assert stages.analyst_saw["baseline"]["failed_tests"] == ["BookingServiceTest.cancel"]


def test_baseline_that_cannot_run_gives_up_before_any_model_stage():
    class DockerDown(FakeStages):
        def baseline(self, state):
            self.calls.append("baseline")
            raise AgentGaveUp("Docker-демон недоступен: Verification невозможна")

    stages = DockerDown()

    final, _ = run(stages, BUG)

    assert stages.calls == ["prepare", "baseline", "cleanup"]
    assert final["outcome"] == "gave_up"
    assert "Docker" in final["outcome_reason"]


def test_red_baseline_searches_open_issues_by_failed_tests_and_exceptions():
    stages = FakeStages(baseline=RED, open_issues=[issue(n) for n in range(1, 13)])

    run(stages, BUG)

    [terms] = stages.searched_for
    assert "bookingRightAfterConfirmedOneIsAllowed" in terms
    assert "ResourceException" in terms
    assert stages.analyst_saw is not None
    assert [i["number"] for i in stages.analyst_saw["related_issues"]] == list(range(1, 11))


def test_green_baseline_does_not_search_issues():
    stages = FakeStages(baseline=GREEN)

    run(stages, BUG)

    assert "search_issues" not in stages.calls


def test_failed_issue_search_does_not_stop_the_investigation():
    class GitHubDown(FakeStages):
        def search_issues(self, terms):
            raise GitHubError("gh: HTTP 503")

    stages = GitHubDown(baseline=RED)

    final, _ = run(stages, BUG)

    assert final["outcome"] == "pr_created"
    assert stages.analyst_saw is not None
    assert stages.analyst_saw.get("related_issues", []) == []


def test_insufficient_data_comments_on_the_issue_with_what_is_missing():
    stages = FakeStages(verdict="insufficient_data")

    final, _ = run(stages, FROM_ISSUE)

    assert final["outcome"] == "insufficient_data"
    [(number, body)] = stages.comments
    assert number == 7
    assert "Недостаточно данных" in body
    assert "NPE в BookingService.cancel" in body


def test_bug_report_from_chat_gets_no_issue_comment():
    stages = FakeStages(verdict="insufficient_data")

    run(stages, BUG)

    assert stages.comments == []


def test_blocked_comments_with_failed_tests_and_the_related_issue():
    stages = FakeStages(
        verdict="blocked",
        baseline=RED,
        open_issues=[issue(1, "Нельзя забронировать слот сразу после чужой брони")],
        related_issue=1,
    )

    final, _ = run(stages, FROM_ISSUE)

    assert stages.calls[-2:] == ["comment_on_issue", "cleanup"]
    assert final["outcome"] == "blocked"
    assert final["related_issue"] == 1
    [(number, body)] = stages.comments
    assert number == 7
    assert "Заблокировано" in body
    assert "BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed" in body
    assert "#1" in body


def test_blocked_ignores_a_related_issue_that_the_search_did_not_find():
    stages = FakeStages(verdict="blocked", baseline=RED, open_issues=[], related_issue=42)

    final, _ = run(stages, FROM_ISSUE)

    assert "related_issue" not in final
    [(_, body)] = stages.comments
    assert "#42" not in body


def test_approved_not_a_bug_is_commented_on_the_issue():
    stages = FakeStages(verdict="not_a_bug")

    final, pauses = run(stages, FROM_ISSUE, [APPROVE])

    assert pauses == ["not_a_bug"]
    assert final["outcome"] == "not_a_bug"
    [(number, body)] = stages.comments
    assert number == 7
    assert "Не баг" in body
    assert "NPE в BookingService.cancel" in body
    assert "planner" not in stages.calls


def test_rejected_not_a_bug_ends_without_a_comment():
    stages = FakeStages(verdict="not_a_bug")

    final, _ = run(stages, FROM_ISSUE, [REJECT])

    assert final["outcome"] == "rejected"
    assert stages.comments == []
    assert stages.calls[-1] == "cleanup"


def test_revised_not_a_bug_goes_back_to_analyst_with_the_comment():
    class ChangesMindOnComment(FakeStages):
        def analyst(self, state):
            approval = state.get("not_a_bug_approval")
            self.verdict = "confirmed" if approval else "not_a_bug"
            return super().analyst(state)

    stages = ChangesMindOnComment()

    final, pauses = run(stages, BUG, [revise("NPE есть в логе, смотри cancel"), APPROVE, APPROVE])

    assert pauses == ["not_a_bug", "plan", "fix"]
    assert stages.analyst_saw is not None
    assert stages.analyst_saw["not_a_bug_approval"]["comment"] == "NPE есть в логе, смотри cancel"
    assert final["outcome"] == "pr_created"


def test_rejected_plan_stops_before_any_code_change():
    stages = FakeStages()

    final, pauses = run(stages, BUG, [REJECT])

    assert pauses == ["plan"]
    assert "coder" not in stages.calls
    assert stages.calls[-1] == "cleanup"
    assert final["outcome"] == "rejected"


def test_revised_plan_goes_back_to_planner_with_the_comment():
    stages = FakeStages()

    final, pauses = run(stages, BUG, [revise("сначала напиши тест"), APPROVE, APPROVE])

    assert pauses == ["plan", "plan", "fix"]
    assert stages.calls.count("planner") == 2
    assert stages.planner_saw[1]["plan_approval"]["comment"] == "сначала напиши тест"
    assert final["attempts"] == 1  # a human's revision is not a failed attempt
    assert final["outcome"] == "pr_created"


def test_revised_fix_goes_back_to_planner_with_the_comment():
    stages = FakeStages(critiques=[Critique(passed=True, notes="можно короче")])

    final, pauses = run(
        stages, BUG, [APPROVE, revise("не трогай BookingController"), APPROVE, APPROVE]
    )

    assert pauses == ["plan", "fix", "plan", "fix"]
    again = stages.planner_saw[1]
    assert again["fix_approval"]["comment"] == "не трогай BookingController"
    assert not again["critic_notes"]  # Critic accepted that Fix: its remarks are not a rejection
    assert stages.calls.count("publish") == 1
    assert final["outcome"] == "pr_created"


def test_rejected_fix_is_not_published():
    stages = FakeStages()

    final, _ = run(stages, BUG, [APPROVE, REJECT])

    assert "publish" not in stages.calls
    assert stages.calls[-1] == "cleanup"
    assert final["outcome"] == "rejected"


def test_fix_pause_shows_the_diff_and_explanation():
    stages = FakeStages()
    graph = build_graph(stages)
    config: RunnableConfig = {"configurable": {"thread_id": "fix-pause"}}
    graph.invoke(BUG, config)
    graph.invoke(Command(resume=APPROVE), config)

    [pause] = graph.get_state(config).interrupts

    assert pause.value["kind"] == "fix"
    assert pause.value["diff"] == "--- a\n+++ b\n"
    assert pause.value["explanation"] == "добавил проверку"
    assert pause.value["reproduced"] is False
    assert pause.value["reproduction_build"] is None


def test_fix_pause_shows_the_reproduction_on_the_original_code():
    stages = FakeStages(reproduction=RED_REPRODUCTION)
    graph = build_graph(stages)
    config: RunnableConfig = {"configurable": {"thread_id": "fix-pause-reproduced"}}
    graph.invoke(BUG, config)
    graph.invoke(Command(resume=APPROVE), config)

    [pause] = graph.get_state(config).interrupts

    assert pause.value["reproduced"] is True
    assert pause.value["reproduction_build"] == RED_REPRODUCTION


def test_fix_pr_for_an_issue_says_fixes_and_is_honest_about_reproduction():
    stages = FakeStages()

    run(stages, FROM_ISSUE)

    [(title, body)] = stages.published
    assert title.startswith("Fix: Отмена брони падает с NPE")
    assert "Fixes #7" in body
    assert "NPE в BookingService.cancel" in body  # the cause found by Analyst
    assert "добавил проверку" in body
    assert "Unreproduced Fix" in body
    assert "агент" in body.lower()


def test_fix_pr_of_a_reproduced_fix_gives_both_builds():
    stages = FakeStages(reproduction=RED_REPRODUCTION)

    run(stages, BUG)

    [(_, body)] = stages.published
    assert "Reproduced Fix" in body
    assert "Unreproduced" not in body
    assert "без Fix: код возврата 1" in body
    assert "с Fix: код возврата 0" in body


def test_fix_pr_for_a_chat_bug_report_quotes_it():
    stages = FakeStages()

    run(stages, BUG)

    [(_, body)] = stages.published
    assert "> Отмена брони падает с NPE" in body
    assert "Fixes #" not in body


def test_failed_publication_gives_up_and_still_cleans_up():
    class PushRejected(FakeStages):
        def publish(self, state, title, body):
            self.calls.append("publish")
            raise PublishError("git push: permission denied")

    stages = PushRejected()

    final, _ = run(stages, BUG)

    assert final["outcome"] == "gave_up"
    assert "permission denied" in final["outcome_reason"]
    assert stages.calls[-1] == "cleanup"


def test_failed_issue_comment_keeps_the_verdict_and_says_so():
    class GitHubDown(FakeStages):
        def comment_on_issue(self, number, body):
            raise GitHubError("gh: HTTP 403")

    stages = GitHubDown(verdict="insufficient_data")

    final, _ = run(stages, FROM_ISSUE)

    assert final["outcome"] == "insufficient_data"
    assert "HTTP 403" in final["outcome_reason"]


def test_blocked_verdict_on_a_green_baseline_is_not_trusted():
    stages = FakeStages(verdict="blocked", baseline=GREEN)

    final, _ = run(stages, FROM_ISSUE)

    assert final["outcome"] == "gave_up"
    assert stages.comments == []


def test_issues_written_in_business_terms_reach_analyst_as_latest_open_ones():
    # Nobody names `bookingRightAfterConfirmedOneIsAllowed` in an issue; Analyst
    # matches "Нельзя забронировать слот сразу после чужой брони" by meaning.
    stages = FakeStages(
        baseline=RED,
        open_issues=[],
        recent_issues=[issue(6), issue(1, "Нельзя забронировать слот сразу после чужой брони")],
    )

    run(stages, BUG)

    assert stages.analyst_saw is not None
    assert [i["number"] for i in stages.analyst_saw["related_issues"]] == [6, 1]


def test_issues_found_by_name_come_first_and_latest_ones_fill_up_without_duplicates():
    stages = FakeStages(
        baseline=RED,
        open_issues=[issue(3)],
        recent_issues=[issue(n) for n in range(20, 0, -1)],
    )

    run(stages, BUG)

    assert stages.analyst_saw is not None
    numbers = [i["number"] for i in stages.analyst_saw["related_issues"]]
    assert numbers == [3, 20, 19, 18, 17, 16, 15, 14, 13, 12]


def test_the_bug_reports_own_issue_is_not_offered_as_related():
    stages = FakeStages(baseline=RED, open_issues=[issue(7)], recent_issues=[issue(7), issue(1)])

    run(stages, FROM_ISSUE)

    assert stages.analyst_saw is not None
    assert [i["number"] for i in stages.analyst_saw["related_issues"]] == [1]
