"""Verify the boundary between Mobile-Agent actions and eval AndroidWorld."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mobile_agent_env import MobileAgentEnv


def test_coordinate_swipe_preserves_endpoints_and_duration(monkeypatch):
    from mobile_agent_env import adb_utils

    env = SimpleNamespace(controller=object(), execute_action=Mock())
    issue = Mock()
    monkeypatch.setattr(adb_utils, 'issue_generic_request', issue)
    monkeypatch.setattr('mobile_agent_env.time.sleep', Mock())
    action = SimpleNamespace(action_type='swipe', direction=[30, 40, 500, 1000])
    MobileAgentEnv(env).execute_action(action)
    issue.assert_called_once_with(
        adb_utils.generate_swipe_command(30, 40, 500, 1000, 500), env.controller
    )
    env.execute_action.assert_not_called()


def test_standard_actions_delegate_without_conversion():
    action = SimpleNamespace(action_type='input_text', text='Hugo Pereira')
    env = SimpleNamespace(execute_action=Mock(return_value='executed'))
    assert MobileAgentEnv(env).execute_action(action) == 'executed'
    env.execute_action.assert_called_once_with(action)


def test_environment_state_and_controller_are_delegated():
    env = SimpleNamespace(controller=object(), get_state=Mock(return_value='screen'))
    adapter = MobileAgentEnv(env)
    assert adapter.controller is env.controller
    assert adapter.get_state(wait_to_stabilize=True) == 'screen'
    env.get_state.assert_called_once_with(wait_to_stabilize=True)
