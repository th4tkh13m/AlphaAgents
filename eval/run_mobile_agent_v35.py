# Copyright 2024 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluate preserved Mobile-Agent v3.5 with the eval AndroidWorld checkout.

Adapted from the upstream run_ma35.py; benchmark code stays outside the harness.
"""

from collections.abc import Sequence
import os
from functools import partial
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness/mobile-gui"))
sys.path.insert(0, str(ROOT / "eval/android_world"))

from absl import app
from absl import flags
from absl import logging
from android_world import checkpointer as checkpointer_lib
from android_world import registry
from android_world import suite_utils
from android_world.agents import base_agent
from mobile_agent import infer_ma3
from mobile_agent import mobile_agent_v3
from mobile_agent_env import MobileAgentEnv
from android_world.env import env_launcher
from android_world.env import interface

logging.set_verbosity(logging.WARNING)

os.environ['GRPC_VERBOSITY'] = 'ERROR'  # Only show errors
os.environ['GRPC_TRACE'] = 'none'  # Disable tracing

def _find_adb_directory() -> str:
  """Returns the directory where adb is located."""
  potential_paths = [
      shutil.which('adb') or '',
      str(ROOT / 'eval/android-sdk/platform-tools/adb'),
      os.path.expanduser('~/Library/Android/sdk/platform-tools/adb'),
      os.path.expanduser('~/Android/Sdk/platform-tools/adb'),
      '/root/android/android_sdk/platform-tools/adb',
  ]
  for path in potential_paths:
    if os.path.isfile(path):
      return path
  raise EnvironmentError(
      'adb not found in the common Android SDK paths. Please install Android'
      " SDK and ensure adb is in one of the expected directories. If it's"
      ' already installed, point to the installed location.'
  )

_ADB_PATH = flags.DEFINE_string(
    'adb_path',
    _find_adb_directory(),
    'Path to adb. Set if not installed through SDK.',
)
_EMULATOR_SETUP = flags.DEFINE_boolean(
    'perform_emulator_setup',
    False,
    'Whether to perform emulator setup. This must be done once and only once'
    ' before running Android World. After an emulator is setup, this flag'
    ' should always be False.',
)
_DEVICE_CONSOLE_PORT = flags.DEFINE_integer(
    'console_port',
    5554,
    'The console port of the running Android device. This can usually be'
    ' retrieved by looking at the output of `adb devices`. In general, the'
    ' first connected device is port 5554, the second is 5556, and'
    ' so on.',
)
_GRPC_PORT = flags.DEFINE_integer(
    'grpc_port',
    8554,
    'grpc_port',
)
_MODEL = flags.DEFINE_string(
    'model',
    '',
    'Your model name.',
)
_SUITE_FAMILY = flags.DEFINE_enum(
    'suite_family',
    registry.TaskRegistry.ANDROID_WORLD_FAMILY,
    [
        # Families from the paper.
        registry.TaskRegistry.ANDROID_WORLD_FAMILY,
        registry.TaskRegistry.MINIWOB_FAMILY_SUBSET,
        # Other families for more testing.
        registry.TaskRegistry.MINIWOB_FAMILY,
        registry.TaskRegistry.ANDROID_FAMILY,
        registry.TaskRegistry.INFORMATION_RETRIEVAL_FAMILY,
    ],
    'Suite family to run. See registry.py for more information.',
)
_TASK_RANDOM_SEED = flags.DEFINE_integer(
    'task_random_seed', 30, 'Random seed for task randomness.'
)
_TASKS = flags.DEFINE_list(
    'tasks',
    None,
    'List of specific tasks to run in the given suite family. If None, run all'
    ' tasks in the suite family.',
)
_N_TASK_COMBINATIONS = flags.DEFINE_integer(
    'n_task_combinations',
    1,
    'Number of task instances to run for each task template.',
)
_CHECKPOINT_DIR = flags.DEFINE_string(
    'checkpoint_dir',
    '',
    'The directory to save checkpoints and resume evaluation from. If the'
    ' directory contains existing checkpoint files, evaluation will resume from'
    ' the latest checkpoint. If the directory is empty or does not exist, a new'
    ' directory will be created.',
)
_OUTPUT_PATH = flags.DEFINE_string(
    'output_path',
    os.path.expanduser('~/android_world/runs'),
    'The path to save results to if not resuming from a checkpoint is not'
    ' provided.',
)
_TRAJ_OUTPUT_PATH = flags.DEFINE_string(
    'traj_output_path',
    '',
    'The path to save traj'
)
_API_KEY = flags.DEFINE_string(
    'api_key',
    '',
    'Your api key'
)
_BASE_URL = flags.DEFINE_string(
    'base_url',
    '',
    'Your base url'
)
_LLM_TIMEOUT = flags.DEFINE_float('llm_timeout', 30.0, 'OpenAI request timeout; upstream default is 30 seconds.')
_DISABLE_THINKING = flags.DEFINE_boolean('disable_thinking', False, 'Send Qwen chat_template_kwargs.enable_thinking=False; defaults to upstream model behavior.')
_LLM_MAX_TOKENS = flags.DEFINE_integer('llm_max_tokens', 200000, 'Generation token limit; default 200,000 tokens.')

# Agent specific.
_AGENT_NAME = flags.DEFINE_string(
    'agent_name',
    'm3a_gpt4v',
    'Agent name.'
)
_FIXED_TASK_SEED = flags.DEFINE_boolean(
    'fixed_task_seed',
    False,
    'Whether to use the same task seed when running multiple task combinations'
    ' (n_task_combinations > 1).',
)

def _get_agent(
    env: interface.AsyncEnv,
    family: str | None = None,
) -> base_agent.EnvironmentInteractingAgent:
  """Gets agent."""
  print('Initializing agent...')
  agent = None
  if _AGENT_NAME.value == 'mobile_agent_v3':
    agent = mobile_agent_v3.MobileAgentV3_M3A(MobileAgentEnv(env), infer_ma3.GUIOwlWrapper(_API_KEY.value, _BASE_URL.value, _MODEL.value), output_path=(_TRAJ_OUTPUT_PATH.value))
  
  if not agent:
    raise ValueError(f'Unknown agent: {_AGENT_NAME.value}')

  # Provider configuration belongs to evaluation.
  agent.vllm.bot = agent.vllm.bot.with_options(timeout=_LLM_TIMEOUT.value)
  if _TRAJ_OUTPUT_PATH.value:
    agent.vllm.response_log_path = str(Path(_TRAJ_OUTPUT_PATH.value) / 'llm_responses.jsonl')
  request_options = {}
  if _LLM_MAX_TOKENS.value:
    request_options['max_tokens'] = _LLM_MAX_TOKENS.value
  if _DISABLE_THINKING.value:
    request_options['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
  if request_options:
    agent.vllm.bot.chat.completions.create = partial(agent.vllm.bot.chat.completions.create, **request_options)
  print('Model request settings:', {'timeout': _LLM_TIMEOUT.value, **request_options})
  agent.name = _AGENT_NAME.value

  return agent

def _main() -> None:
  """Runs eval suite and gets rewards back."""
  env = env_launcher.load_and_setup_env(
      console_port=_DEVICE_CONSOLE_PORT.value,
      emulator_setup=_EMULATOR_SETUP.value,
      adb_path=_ADB_PATH.value,
      grpc_port=_GRPC_PORT.value
  )

  n_task_combinations = _N_TASK_COMBINATIONS.value
  print("n_task_combinations: ", n_task_combinations)
  task_registry = registry.TaskRegistry()
  suite = suite_utils.create_suite(
      task_registry.get_registry(family=_SUITE_FAMILY.value),
      n_task_combinations=n_task_combinations,
      seed=_TASK_RANDOM_SEED.value,
      tasks=_TASKS.value,
      use_identical_params=_FIXED_TASK_SEED.value,
  )
  suite.suite_family = _SUITE_FAMILY.value
  agent = _get_agent(env, _SUITE_FAMILY.value)
  agent.get_task_name(suite)
  agent.transition_pause = None

  if _CHECKPOINT_DIR.value:
    checkpoint_dir = _CHECKPOINT_DIR.value
  else:
    checkpoint_dir = checkpointer_lib.create_run_directory(_OUTPUT_PATH.value)

  print(
      f'Starting eval with agent {_AGENT_NAME.value} and writing to'
      f' {checkpoint_dir}'
  )
  suite_utils.run(
      suite,
      agent,
      checkpointer=checkpointer_lib.IncrementalCheckpointer(checkpoint_dir),
      demo_mode=False,
  )
  print(
      f'Finished running agent {_AGENT_NAME.value} on {_SUITE_FAMILY.value}'
      f' family. Wrote to {checkpoint_dir}.'
  )
  env.close()

def main(argv: Sequence[str]) -> None:
  del argv
  _main()

if __name__ == '__main__':
  app.run(main)
