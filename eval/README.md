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
