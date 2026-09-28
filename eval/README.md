# Mobile evaluation environments

The official [AndroidWorld](https://github.com/google-research/android_world) and [MobileWorld](https://github.com/Tongyi-MAI/MobileWorld) source trees are included as Git submodules at the revisions below. Clone this repository with `git clone --recurse-submodules`, or run `git submodule update --init` after cloning. Run `./eval/setup.sh` from the repository root to initialize both submodules and install separate Python environments. Virtual environments, emulator files, credentials, and run results remain local artifacts.

| Project | Checkout | Python | Revision |
| --- | --- | --- | --- |
| AndroidWorld | `android_world/` | 3.11 | `e3fea3ccc69787570e282c99573298f1c3019a34` |
| MobileWorld | `MobileWorld/` | 3.12 | `e41d1478e252325c513003d3d191b4c164b4af2c` |

For a fresh host, install Android SDK command-line tools, `adb`, `uv`, and Docker first. Then run `./eval/setup.sh --with-emulator --pull-mobile-image` for the full setup. The emulator option downloads Android API 33; the image option pulls MobileWorld's large Docker image. `setup.sh` applies `android_world_adb_path.patch` to let AndroidWorld find `adb` on `PATH`.

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
