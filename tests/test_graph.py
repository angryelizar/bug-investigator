from bug_investigator.graph import build_graph
from bug_investigator.stages import AgentGaveUp, Analysis, Critique, Fix, Workspace
from bug_investigator.state import BuildResult, InvestigationState, Verdict

BUG = {"bug_report": {"text": "Отмена брони падает с NPE", "source": "chat"}}


class FakeStages:
    """Scripted stages: each critic/build call takes the next scripted answer."""

    def __init__(self, critiques=None, builds=None, verdict: Verdict = "confirmed"):
        self.critiques = list(critiques or [Critique(passed=True, notes="")])
        self.builds = list(builds or [BuildResult(exit_code=0, log_tail="BUILD SUCCESS")])
        self.verdict: Verdict = verdict
        self.calls: list[str] = []
        self.planner_saw_notes: list[str | None] = []

    def prepare(self, state):
        self.calls.append("prepare")
        return Workspace(workdir="/tmp/wc", branch="bug-investigator/abc")

    def analyst(self, state):
        self.calls.append("analyst")
        return Analysis(verdict=self.verdict, summary="NPE в BookingService.cancel")

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

    assert stages.calls == ["prepare", "analyst", "planner", "coder", "build", "critic"]
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

    assert stages.calls == ["prepare", "analyst"]
    assert final["outcome"] == "insufficient_data"
    assert final["outcome_reason"] == "NPE в BookingService.cancel"


def test_stage_that_gives_up_ends_investigation_as_gave_up():
    class CoderHitsToolLimit(FakeStages):
        def coder(self, state):
            self.calls.append("coder")
            raise AgentGaveUp("Coder исчерпал 15 вызовов инструментов")

    stages = CoderHitsToolLimit()

    final = build_graph(stages).invoke(BUG)

    assert stages.calls == ["prepare", "analyst", "planner", "coder"]
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
