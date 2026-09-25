"""Loader shim for eval_config.yaml custom metrics.

agents-cli exec()s each `custom_function` in an empty namespace (no __file__), so each
metric in eval_config.yaml runs a two-line shim that execs this file and calls
`check("<metric>")`. Modules are loaded once into sys.modules so all metrics share
state (e.g. the LLM-judge cache). Run agents-cli from the project root (the directory
containing tests/eval/).
"""

import importlib.util
import pathlib
import sys


def _metrics_dir() -> pathlib.Path:
    cwd = pathlib.Path.cwd()
    for base in (cwd, *cwd.parents):
        d = base / "tests" / "eval" / "metrics"
        if (d / "hr_checks.py").exists():
            return d
    raise FileNotFoundError("tests/eval/metrics not found; run agents-cli from the project root")


def _module(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _metrics_dir() / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check(metric: str):
    for mod_name in ("hr_checks", "hr_judge"):
        mod = _module(mod_name)
        if metric in mod.CHECKS:
            return mod.CHECKS[metric]
    raise KeyError(metric)
