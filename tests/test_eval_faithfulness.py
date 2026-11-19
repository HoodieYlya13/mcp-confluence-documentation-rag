from itertools import cycle
from typing import Iterator, List

import pytest
from src.config import SecurityRoles
from src.eval_suite import EvaluationSuite


class ScriptedJudgeSuite(EvaluationSuite):
    def __init__(self, verdicts: List[bool]) -> None:
        super().__init__()
        self._scripted_verdicts: Iterator[bool] = cycle(verdicts)

    def _judge_verdict(self, context: str, answer: str) -> bool:
        return next(self._scripted_verdicts)


@pytest.mark.parametrize(
    ("verdicts", "expected_faithful"),
    [
        ([True, False, True], True),
        ([False, True, False], False),
        ([True, True, True], True),
    ],
)
def test_case_is_faithful_by_majority_of_judge_votes(verdicts, expected_faithful) -> None:
    suite = ScriptedJudgeSuite(verdicts)

    is_faithful = suite._is_answer_faithful(
        "What are the pressure limits and warnings for Sector 3-4?", SecurityRoles.JUNIOR_OP
    )

    assert is_faithful is expected_faithful
