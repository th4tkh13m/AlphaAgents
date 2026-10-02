"""Translate Mobile-Agent's coordinate swipe API to the eval environment."""

import time

from android_world.env import adb_utils


class MobileAgentEnv:
    """Delegate environment operations; preserve upstream swipe coordinates."""

    def __init__(self, env):
        self._env = env

    def __getattr__(self, name):
        return getattr(self._env, name)

    def execute_action(self, action):
        if action.action_type == 'swipe' and isinstance(action.direction, list):
            start_x, start_y, end_x, end_y = action.direction
            command = adb_utils.generate_swipe_command(
                int(start_x), int(start_y), int(end_x), int(end_y), 500
            )
            adb_utils.issue_generic_request(command, self._env.controller)
            time.sleep(2)
            return
        return self._env.execute_action(action)
