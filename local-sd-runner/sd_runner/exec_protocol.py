"""Finite palette work, invoked only inside the runner's owned supervisor."""

from sd_db import runner_exec


def plan(descriptor, *, home=None):
    return runner_exec.process_plan(descriptor, home=home)


def run(descriptor, *, cwd, home=None):
    return runner_exec.run_process(descriptor, cwd=cwd, home=home, timeout=86400)
