# Mobile evaluation environments

The official [AndroidWorld](https://github.com/google-research/android_world) and [MobileWorld](https://github.com/Tongyi-MAI/MobileWorld) source trees are included as Git submodules at the revisions below. Clone this repository with `git clone --recurse-submodules`, or run `git submodule update --init` after cloning. Run `./eval/setup.sh` from the repository root to initialize both submodules and install separate Python environments. Virtual environments, emulator files, credentials, and run results remain local artifacts.

| Project | Checkout | Python | Revision |
| --- | --- | --- | --- |
| AndroidWorld | `android_world/` | 3.11 | `e3fea3ccc69787570e282c99573298f1c3019a34` |
| MobileWorld | `MobileWorld/` | 3.12 | `e41d1478e252325c513003d3d191b4c164b4af2c` |

For a fresh host, install Android SDK command-line tools, `adb`, `uv`, and Docker first. Then run `./eval/setup.sh --with-emulator --pull-mobile-image` for the full setup. The emulator option downloads Android API 33; the image option pulls MobileWorld's large Docker image. `setup.sh` applies `android_world_adb_path.patch` and `android_world_grpc_port.patch` so AndroidWorld finds `adb` on `PATH` and accepts a per-run gRPC port.

## AndroidWorld

The local `AndroidWorldAvd` is a Pixel 6 running Android API 33. The Android SDK and AVD are under this directory. Start the emulator in one terminal:

```bash
cd eval
ANDROID_HOME="$PWD/android-sdk" ANDROID_SDK_ROOT="$PWD/android-sdk" \
ANDROID_AVD_HOME="$PWD/.android/avd" \
./android-sdk/emulator/emulator -avd AndroidWorldAvd -no-snapshot -grpc 8554
```

For a headless host, add `-no-window -no-audio -no-metrics`. In another terminal, run a task after configuring the model credentials your agent needs:

```bash
cd eval/android_world
./.venv/bin/python run.py --suite_family=android_world \
  --agent_name=t3a_gpt4 --perform_emulator_setup \
  --tasks=ContactsAddContact
```

Use `--perform_emulator_setup` on the first run to install the benchmark apps, then omit it. To select the SMS task, replace `--tasks=ContactsAddContact` with `--tasks=SimpleSmsSend`.

### Parallel AndroidWorld evaluations

The [Android emulator](https://developer.android.com/studio/run/emulator-commandline) gives each AVD its own writable data and each running emulator its own console/ADB ports. AndroidWorld's [setup instructions](https://github.com/google-research/android_world#installation) require a gRPC port as well. The launcher below creates one Pixel 6/API 33 AVD per worker, assigns unique console/ADB and gRPC ports, gives each process a separate temporary directory and result directory, and stops only the emulators it started.

```bash
./eval/setup.sh --with-emulator
eval/android_world/.venv/bin/python eval/run_android_world_parallel.py \
  --workers 2 --agent-name t3a_gpt4
```

The default `shard` mode divides AndroidWorld task names among the workers. To run the same selected tasks independently on every worker, use `--mode replicate --tasks ClockStopWatchRunning,ContactsAddContact`; replicate mode gives each worker a different task seed. `--n-task-combinations` still controls the number of parameter variations **within** each worker. Choose another free even console port with `--base-console-port` if 5554 is occupied, and another free gRPC range with `--base-grpc-port` if 8554 is occupied.

To run separate evaluation commands at the same time (for example, different agents), give each command a different `--avd-prefix` and disjoint console/ADB and gRPC port ranges. A console port uses the following odd port for ADB; each additional worker uses the next even/odd pair. For example, one command can use `--avd-prefix AgentA --base-console-port 5560 --base-grpc-port 8560`, and another can use `--avd-prefix AgentB --base-console-port 5570 --base-grpc-port 8570`.

On first use of each AVD, the launcher installs AndroidWorld apps and stores a local setup marker. App setup, emulator, and runner logs are saved under ignored `eval/results/android_world_parallel/`; this initial installation can take several minutes. If AndroidWorld reports an app-specific setup warning, the launcher prints it on later runs too; inspect `app_setup.log` before using that app's tasks. For built-in Android tasks that need no extra apps, `--skip-app-setup` skips that step. `--dry-run` shows the task and port assignments without starting anything. Each worker needs access to the model credentials required by the chosen agent.

## MobileWorld

Python dependencies are installed with `uv sync --python /usr/bin/python3.12 --no-dev`. The host has Docker, KVM, and the required `ghcr.io/tongyi-mai/mobile_world:latest` image. Add real credentials to `MobileWorld/.env` as described in its `.env.example`; at minimum, agent evaluations require `API_KEY`.

```bash
cd eval/MobileWorld
./.venv/bin/mw env check
./.venv/bin/mw env run --count 1
./.venv/bin/mw info task --no-pager
./.venv/bin/mw eval --help
```

The MobileWorld Docker environment uses privileged containers. Its README provides the full evaluation arguments and optional credentials for user interaction and MCP tasks.

## Direct harness evaluation

The local integrations use this directory's AndroidWorld checkout for tasks and rewards:

- `run_mobile_agent_v35.py`: cleaned Mobile-Agent v3.5 from `harness/mobile-gui/mobile_agent`. See `harness/mobile-gui/README.md` for the invocation. Optional `--disable_thinking`, `--llm_timeout`, and `--llm_max_tokens` configure Qwen requests in the evaluation runner. The output limit defaults to 200,000 tokens. The wrapper validates responses and retries failures; prompts and agent decision logic retain upstream behavior.
- `run_artemis_androidworld.py`: unchanged working Artemis from `harness/artemis`. See `harness/artemis/MIGRATION.md` for its Python 3.12 dependency environment and invocation. `artemis_androidworld_qwen.jsonc` selects the local Qwen model for every role.

Supply matching emulator console/gRPC ports. The default console/ADB ports may be occupied by other local services. For an isolated headless emulator, add `-port 5560 -grpc 8560 -no-window -no-audio -no-metrics` to the startup command above. Use separate AVDs and port pairs when evaluating agents concurrently.

The evaluation-only `mobile_agent_env.py` preserves Mobile-Agent's coordinate-based swipe endpoints and 500 ms duration. Other actions and environment operations delegate directly to AndroidWorld. Its tests run with `eval/android_world/.venv/bin/python -m pytest -q eval/tests/test_mobile_agent_env.py`.

### Harness verification on 2026-09-30

Two tasks were evaluated against AndroidWorld revision `e3fea3ccc69787570e282c99573298f1c3019a34` using `Qwen/Qwen3.8-27B-FP8` at `http://localhost:8001/v1`. This is a smoke evaluation, not a full benchmark or a controlled agent comparison (Mobile-Agent seed 30; Artemis seed 42).

| Harness | ClockStopWatchRunning | ContactsAddContact |
| --- | --- | --- |
| Mobile-Agent v3.5, Qwen thinking disabled | Reward 1.0, 4 steps | Reward 0.0, 12-step limit |
| Artemis, source default agent config | Reward 1.0 | Reward 1.0 |

The configured Mobile-Agent run finished both episodes without exceptions. Its upstream-default Qwen run encountered malformed reflector responses on both tasks; the successful smoke used `--disable_thinking --llm_timeout=180 --llm_max_tokens=4096` in the evaluation runner. Artemis completed both tasks without exceptions, while its default transcript/visual summary paths emitted missing Gemini credential warnings and fell back to existing behavior.

All 67 repository regression tests and 3 evaluation adapter tests passed. The coordinate swipe adapter also changed the screen through the real eval AndroidWorld environment. A source audit confirmed all 797 copied Artemis files were identical to `/data/khiem/Reproduce/artemis`, and the four retained Mobile-Agent modules changed only imports. Bundled benchmark code was removed; upstream task-specific agent hints remain as requested.

Detailed manifests, checkpoints, screenshots, traces, logs, source hashes, and `results_summary.json` are local artifacts under ignored `eval/results/harness_validation/`.
