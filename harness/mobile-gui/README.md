# Mobile-Agent v3.5 for AndroidWorld

Copied directly from [X-PLUG/MobileAgent](https://github.com/X-PLUG/MobileAgent/tree/11cea575561fb7800b5fb6b6cafa56f7a91de11f/Mobile-Agent-v3.5/android_world_v3.5), commit `11cea575561fb7800b5fb6b6cafa56f7a91de11f`.

## Included source

- `run_ma35.py` and `run_ma35.sh`: official Mobile-Agent v3.5 evaluation entry points.
- `android_world/`: Python dependencies reachable from that runner, including the agent, environment, task registry, evaluation utilities, task metadata, and protobuf sources/data.
- `setup.py` and `requirements.txt`: upstream installation and dependency files.
- `LICENSE`: AndroidWorld Apache license; `LICENSE.mobileagent`: repository MIT license.

Copied upstream files are unchanged. The runner imports GUI-Owl and the registry imports additional task families, so their imported dependencies are retained. Unrelated agent platforms, unreferenced modules, upstream tests, Docker/server tooling, app build sources, and documentation assets are omitted. App APKs and task data are downloaded by AndroidWorld setup when needed.

## Install and run

Use Linux/WSL with Python 3.11, Android SDK/ADB, and a configured Pixel 6 Android API 33 emulator. Follow the [upstream AndroidWorld setup](https://github.com/X-PLUG/MobileAgent/tree/11cea575561fb7800b5fb6b6cafa56f7a91de11f/Mobile-Agent-v3.5/android_world_v3.5#installation).

From this directory, install the upstream dependencies and generate protobuf bindings:

```bash
python -m pip install -r requirements.txt
python setup.py generate_protos
```

Start your emulator with console port 5554 and gRPC port 8554. Edit the model name, API key, and base URL in `run_ma35.sh`, then run:

```bash
bash run_ma35.sh
```

Add `--perform_emulator_setup` to the Python command on the first run. The upstream runner also accepts `--tasks` to select tasks.

This is the official local ADB/gRPC runner. The tree-search AndroidWorld bridge currently expects the custom `scripts/run_ma35_on_docker.py` HTTP runner; that runner is not part of this upstream copy. Connecting this source to that bridge requires a separate adapter change.
