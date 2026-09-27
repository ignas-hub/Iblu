"""Golden-day scenarios — the reconstructed day and the questions, run
through the REAL pipeline (`iblu_keeper.testing.scenarios`), not re-tested at
the level of dicts and internals. See that module's docstring for why this
file exists and `iblu_keeper.testing.scenario_data` for the scenarios
themselves.

Each scenario is its own named test (parametrised by scenario name), so a
regression is a single red test naming the exact defect it protects, not a
diff buried in a 1000-line assertion file.
"""

from __future__ import annotations

import pytest

from iblu_keeper.testing import scenarios as harness
from iblu_keeper.testing.scenario_data import QUESTION_SCENARIOS, SCENARIOS


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_day_scenario(scenario):
    failures = harness.check(scenario)
    assert not failures, "\n".join(f"{scenario.name}: {f}" for f in failures)


@pytest.mark.parametrize("scenario", QUESTION_SCENARIOS, ids=[s.name for s in QUESTION_SCENARIOS])
def test_question_scenario(scenario):
    failures = harness.check_questions(scenario)
    assert not failures, "\n".join(f"{scenario.name}: {f}" for f in failures)
