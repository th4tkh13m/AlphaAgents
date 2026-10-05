"""Configure invocation limits in the evaluator process, preserving source files."""

import inspect


def configure_llm_timeout(invoke, seconds):
    """Update the shared function's default, including already imported aliases.

    Artemis callers import this function directly, so replacing the module
    attribute would leave those callers using the old limit. Explicit per-call
    overrides remain respected.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise ValueError("LLM invocation timeout must be a positive integer")
    parameters = list(inspect.signature(invoke).parameters.values())
    defaults = invoke.__defaults__
    if not defaults:
        raise ValueError("Artemis invocation helper has no configurable defaults")
    default_parameters = parameters[len(parameters) - len(defaults):]
    names = [parameter.name for parameter in default_parameters]
    if "hard_timeout" not in names:
        raise ValueError("Artemis invocation helper lacks the hard_timeout default")
    values = list(defaults)
    values[names.index("hard_timeout")] = seconds
    invoke.__defaults__ = tuple(values)
    return seconds
