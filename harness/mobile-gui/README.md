# Mobile-Agent v3.5

Agent source from [X-PLUG/MobileAgent](https://github.com/X-PLUG/MobileAgent/tree/11cea575561fb7800b5fb6b6cafa56f7a91de11f/Mobile-Agent-v3.5/android_world_v3.5), commit `11cea575561fb7800b5fb6b6cafa56f7a91de11f`.

`mobile_agent/` contains the manager, executor, reflector, notetaker, inference wrapper, and action schema. Agent prompts, planning, action selection, and task-specific heuristics are preserved by request. Imports point to the new package. The inference wrapper adds bounded retries for empty, incomplete, or invalid reflector responses. This cleanup removes bundled benchmark definitions and evaluator data; it does not claim the upstream prompts are free of task-specific knowledge.

The former `android_world/` tree, task metadata, registry, task generators, validators, checkpoints, environment implementation, and benchmark packaging have been removed. AndroidWorld is supplied exclusively by `eval/android_world`; shared agent/environment interfaces come from that checkout. Evaluation entry points and the coordinate-swipe adapter live under `eval/`.

From the repository root:

```bash
uv pip install --python eval/android_world/.venv/bin/python -r harness/mobile-gui/requirements.txt
eval/android_world/.venv/bin/python eval/run_mobile_agent_v35.py \
  --agent_name=mobile_agent_v3 --tasks=ClockStopWatchRunning,ContactsAddContact \
  --model=Qwen/Qwen3.8-27B-FP8 --api_key=EMPTY \
  --base_url=http://localhost:8001/v1 --console_port=5560 --grpc_port=8560 \
  --output_path="$PWD/eval/results/mobile_agent" \
  --traj_output_path="$PWD/eval/results/mobile_agent_traces"
```

The evaluation runner sets the output limit to 200,000 tokens by default, as requested. Use `--llm_timeout=180` for longer reasoning responses; thinking mode can remain enabled. The agent reads `message.content`. Empty responses, unexpected finish reasons, and invalid reflector output retry up to 10 times; exhaustion raises `LlmResponseError`. Raw responses and retry errors are saved in `llm_responses.jsonl` under the trajectory output directory. Reasoning content is logged for diagnosis and is not used as the answer.

Start an AndroidWorld emulator first; see `eval/README.md`. Use matching console/gRPC ports and `--perform_emulator_setup` if benchmark apps have not yet been installed. Results and screenshots belong under `eval/results/`.

`LICENSE` retains AndroidWorld's Apache license; `LICENSE.mobileagent` retains the MobileAgent repository's MIT license.
