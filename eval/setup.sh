#!/usr/bin/env bash
set -euo pipefail

eval_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(dirname -- "$eval_dir")
android_world_revision=e3fea3ccc69787570e282c99573298f1c3019a34
mobile_world_revision=e41d1478e252325c513003d3d191b4c164b4af2c
with_emulator=false
pull_mobile_image=false

for arg in "$@"; do
  case "$arg" in
    --with-emulator) with_emulator=true ;;
    --pull-mobile-image) pull_mobile_image=true ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

git -C "$repo_dir" submodule update --init -- eval/android_world eval/MobileWorld
for project in android_world MobileWorld; do
  if [[ "$project" == android_world ]]; then
    revision=$android_world_revision
  else
    revision=$mobile_world_revision
  fi
  if [[ $(git -C "$eval_dir/$project" rev-parse HEAD) != "$revision" ]]; then
    echo "Unexpected $project revision; expected $revision" >&2
    exit 1
  fi
done

for patch in android_world_adb_path.patch android_world_grpc_port.patch; do
  if ! git -C "$eval_dir/android_world" apply --reverse --unidiff-zero --check \
    "$eval_dir/$patch" 2>/dev/null; then
    git -C "$eval_dir/android_world" apply --unidiff-zero "$eval_dir/$patch"
  fi
done

if [[ ! -x "$eval_dir/android_world/.venv/bin/python" ]]; then
  uv venv --python 3.11 "$eval_dir/android_world/.venv"
fi
uv pip install --python "$eval_dir/android_world/.venv/bin/python" \
  -r "$eval_dir/android_world/requirements.txt" -e "$eval_dir/android_world"
(
  cd "$eval_dir/MobileWorld"
  uv sync --python 3.12 --no-dev --frozen
)

if $with_emulator; then
  host_sdk=${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}
  if [[ -z "$host_sdk" ]]; then
    adb_path=$(command -v adb)
    host_sdk=$(dirname -- "$(dirname -- "$(readlink -f -- "$adb_path")")")
  fi
  if [[ ! -x "$host_sdk/cmdline-tools/latest/bin/sdkmanager" ]]; then
    echo "Android command-line tools not found under $host_sdk" >&2
    exit 1
  fi
  local_sdk="$eval_dir/android-sdk"
  mkdir -p "$local_sdk/cmdline-tools" "$eval_dir/.android/avd"
  if [[ ! -e "$local_sdk/cmdline-tools/latest" ]]; then
    cp -a "$host_sdk/cmdline-tools/latest" "$local_sdk/cmdline-tools/latest"
  fi
  if [[ ! -e "$local_sdk/platform-tools" ]]; then
    ln -s "$host_sdk/platform-tools" "$local_sdk/platform-tools"
  fi
  if [[ ! -f "$local_sdk/system-images/android-33/google_apis/x86_64/package.xml" ]]; then
    "$local_sdk/cmdline-tools/latest/bin/sdkmanager" \
      --sdk_root="$local_sdk" 'system-images;android-33;google_apis;x86_64'
  fi
  if [[ ! -f "$eval_dir/.android/avd/AndroidWorldAvd.avd/config.ini" ]]; then
    printf 'no\n' | env ANDROID_HOME="$local_sdk" \
      ANDROID_SDK_ROOT="$local_sdk" ANDROID_AVD_HOME="$eval_dir/.android/avd" \
      "$local_sdk/cmdline-tools/latest/bin/avdmanager" create avd \
      -n AndroidWorldAvd -k 'system-images;android-33;google_apis;x86_64' \
      -d pixel_6
  fi
fi

if $pull_mobile_image; then
  docker pull ghcr.io/tongyi-mai/mobile_world:latest
fi

echo "Evaluation checkouts and requested dependencies are ready in $eval_dir"
