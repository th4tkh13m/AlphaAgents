"""Bounded readiness checks for an OpenAI-compatible model endpoint."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def wait_for_model(base_url, model, *, attempts=3, timeout=10, retry_delay=5):
    if attempts < 1:
        raise ValueError("Model preflight requires at least one attempt")
    url = base_url.rstrip("/") + "/models"
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(url, timeout=timeout) as response:
                data = json.load(response)
            models = [item["id"] for item in data.get("data", [])]
        except (HTTPError, URLError, OSError, ValueError, KeyError) as error:
            print(
                f"Model preflight attempt {attempt}/{attempts} failed: {type(error).__name__}: {error}",
                flush=True,
            )
            if attempt == attempts:
                raise RuntimeError(
                    f"Model endpoint {base_url} is unavailable after {attempts} attempts"
                ) from error
            time.sleep(retry_delay)
            continue
        if model not in models:
            raise RuntimeError(
                f"Requested model {model!r} is not advertised by {base_url}: {models}"
            )
        print(f"Model preflight ready: {model} at {base_url}", flush=True)
        return models
