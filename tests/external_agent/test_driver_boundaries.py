import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

from clients.external_agent.driver import Hold, investigate, result_data

from .test_driver import BINDING, CONTEXT, Conductor, Session


def test_wrong_conductor_stage_creates_no_external_issue():
    session = Session([])
    conductor = Conductor()
    conductor.require_diagnosis = Mock(side_effect=Hold("Wrong stage"))
    with TemporaryDirectory() as directory, pytest.raises(Hold):
        asyncio.run(investigate(session, conductor, Path(directory), CONTEXT, BINDING))
    assert session.calls == [] and conductor.answers == []


@pytest.mark.parametrize("budget", [0, -1, 1201, True, 1.5])
def test_invalid_budget_does_not_dispatch(budget):
    session = Session([])
    with TemporaryDirectory() as directory, pytest.raises(Hold):
        asyncio.run(investigate(session, Conductor(), Path(directory), CONTEXT, BINDING, budget_seconds=budget))
    assert session.calls == []


def test_non_object_structured_result_is_rejected():
    with pytest.raises(Hold):
        result_data(Mock(structuredContent=[]))
