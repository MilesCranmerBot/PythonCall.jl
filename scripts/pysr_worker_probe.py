"""Temporary Windows comparison of released PySR and the PythonCall checkout."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import traceback


MODES = ("legacy0934", "pre1362", "post1362", "upstream-fixed")
MARKER = "WORKER_OBSERVATION="
COMPATIBILITY_BRANCH = (
    '    if version("juliacall") == "0.9.35":\n'
    '        os.environ["JULIA_PYTHONCALL_EXE"] = sys.executable or ""\n'
)
TEST = (
    "pysr.test.test_startup.TestStartup."
    "test_juliacall_0935_distributed_worker_uses_current_python"
)
IMPORT_ORDERS = (
    "import pysr\nfrom pysr import jl",
    "import juliacall\nimport pysr\nfrom pysr import jl",
)
WORKER_CODE = r'''
using Distributed
worker = only(addprocs(1, exeflags="--threads=1"))
try
    fetch(Distributed.remotecall_eval(Main, worker, :(using PythonCall)))
    @fetchfrom worker begin
        state = pydict(
            selected_executable = string(PythonCall.python_executable_path()),
            library = string(PythonCall.python_library_path()),
        )
        pyexec("""
import json
import subprocess
import sys
import traceback

observation = {
    "selected_executable": selected_executable,
    "library": library,
    "executable": sys.executable,
    "version": sys.version,
    "prefix": sys.prefix,
    "sum": sum([1, 2, 3]),
}
try:
    result = subprocess.run(
        [sys.executable, "-c", "print(6)"],
        capture_output=True, text=True, timeout=20,
    )
    observation["subprocess"] = {
        "outcome": "passed" if result.returncode == 0 else "failed",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
except Exception as error:
    observation["subprocess"] = {
        "outcome": "exception",
        "error": repr(error),
        "traceback": traceback.format_exc(),
    }
result_json = json.dumps(observation)
""", state)
        pyconvert(String, state["result_json"])
    end
finally
    rmprocs(worker)
end
'''


def setup(mode):
    spec = importlib.util.find_spec("pysr")
    assert spec is not None and spec.submodule_search_locations, "PySR is not installed"
    package = Path(next(iter(spec.submodule_search_locations)))
    source = package / "julia_import.py"
    original = source.read_text(encoding="utf-8")
    assert original.count(COMPATIBILITY_BRANCH) == 1, "Expected exactly one PySR 1362 branch"
    if mode != "post1362":
        source.write_text(original.replace(COMPATIBILITY_BRANCH, ""), encoding="utf-8")
    result = {
        "mode": mode,
        "compatibility_branch_removed": mode != "post1362",
        "pysr_source": str(source),
        "startup_test_sha256": hashlib.sha256(
            (package / "test" / "test_startup.py").read_bytes()
        ).hexdigest(),
    }
    if mode == "upstream-fixed":
        import juliacall
        import juliapkg

        jl = juliacall.Main
        jl.seval("using Pkg")
        project = str(jl.seval("Base.active_project()"))
        assert Path(project).parent.resolve() == Path(juliapkg.project()).resolve()
        assert jl.seval('haskey(Pkg.project().dependencies, "SymbolicRegression")')
        jl.Pkg.develop(path=str(Path(__file__).resolve().parents[1]))
        assert str(jl.seval("Base.active_project()")) == project
        assert jl.seval('haskey(Pkg.project().dependencies, "SymbolicRegression")')
        result["julia_project"] = project
        result["pythoncall_checkout"] = str(Path(__file__).resolve().parents[1])
    text = json.dumps(result, indent=2)
    Path("worker-setup.json").write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)


def observe(import_order):
    import os

    os.environ.pop("JULIA_CONDAPKG_OFFLINE", None)
    observation = {
        "import_order": import_order,
        "parent": {
            "executable": sys.executable,
            "version": sys.version,
            "prefix": sys.prefix,
        },
    }
    try:
        namespace = {}
        exec(import_order, namespace)
        observation["worker"] = json.loads(namespace["jl"].seval(WORKER_CODE))
        worker = observation["worker"]
        observation["checks"] = {
            "current_python": worker["executable"] == sys.executable,
            "selected_current_python": worker["selected_executable"] == sys.executable,
            "current_version": worker["version"] == sys.version,
            "current_prefix": worker["prefix"] == sys.prefix,
            "sum": worker["sum"] == 6,
            "subprocess": (
                worker["subprocess"].get("returncode") == 0
                and worker["subprocess"].get("stdout", "").strip() == "6"
            ),
        }
        observation["outcome"] = (
            "passed" if all(observation["checks"].values()) else "failed"
        )
    except Exception as error:
        observation.update(
            outcome="exception", error=repr(error), traceback=traceback.format_exc()
        )
    print(MARKER + json.dumps(observation), flush=True)
    return 0 if observation["outcome"] == "passed" else 1


def run_child(arguments):
    try:
        result = subprocess.run(
            [sys.executable, *arguments], capture_output=True, text=True, timeout=300
        )
        return {
            "outcome": "passed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "outcome": "timeout",
            "returncode": None,
            "stdout": (error.stdout or b"").decode(errors="replace"),
            "stderr": (error.stderr or b"").decode(errors="replace"),
        }


def probe(mode):
    required_test = mode == "upstream-fixed" or (
        mode == "post1362" and sys.version_info[:2] == (3, 14)
    )
    report = {
        "mode": mode,
        "parent_executable": sys.executable,
        "parent_version": sys.version,
        "unchanged_test_required": required_test,
        "worker_checks_required": mode == "upstream-fixed",
        "unchanged_test": run_child(["-m", "unittest", "-v", TEST]),
        "import_orders": [],
    }
    for order in IMPORT_ORDERS:
        child = run_child([
            str(Path(__file__).resolve()), "--mode", mode, "--observe", order
        ])
        lines = [line for line in child["stdout"].splitlines() if line.startswith(MARKER)]
        if len(lines) == 1:
            child["observation"] = json.loads(lines[0][len(MARKER):])
        report["import_orders"].append(child)
    workers_pass = all(
        child["returncode"] == 0
        and child.get("observation", {}).get("outcome") == "passed"
        for child in report["import_orders"]
    )
    test_pass = report["unchanged_test"]["returncode"] == 0
    report["outcome"] = "passed" if test_pass and workers_pass else "negative"
    report["requirements_met"] = (
        (not required_test or test_pass)
        and (mode != "upstream-fixed" or workers_pass)
    )
    text = json.dumps(report, indent=2)
    Path("worker-observations.json").write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    return 0 if report["requirements_met"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--phase", choices=("setup", "probe"), default="probe")
    parser.add_argument("--observe", choices=IMPORT_ORDERS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.observe is not None:
        sys.exit(observe(args.observe))
    if args.phase == "setup":
        setup(args.mode)
    else:
        sys.exit(probe(args.mode))
