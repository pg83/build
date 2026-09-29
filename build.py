import build

# The runner tests itself through its own graph: one node per unittest
# module, with the module and the file it covers as inputs, so a green module
# stays cached until one of them changes and both modules run in parallel.

build.flags.allow({
    "coverage": {
        "descr": "measure the suite with coverage.py; `./build -Dcoverage coverage` writes $(B)/coverage.xml",
        "default": "",
    },
})

COVERAGE = bool(build.flags.coverage)
# The share of lines in `build` and `style.py` the suite must reach.
COVERAGE_MINIMUM = 60

PYTHON_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def mkdir(path):
    return [
        "python3",
        "-c",
        f"from pathlib import Path; Path(r'{path}').mkdir(parents=True, exist_ok=True)",
    ]


def touch(path):
    return [
        "python3",
        "-c",
        f"from pathlib import Path; p=Path(r'{path}'); p.parent.mkdir(parents=True, exist_ok=True); p.touch()",
    ]


def unit_test(module, covers):
    # A passed suite produces nothing but its verdict, so the stamp is the
    # node's only output; under -Dcoverage the measurements join it.
    stamp = f"$(B)/tests/{module}.stamp"
    outputs = [stamp]
    inputs = [f"$(S)/{module}.py", *covers]
    env = dict(PYTHON_ENV)
    prelude = []
    run = ["python3", "-m", "unittest", "-v", module]
    if COVERAGE:
        # coverage.py measures this process, the child `build` forks to do
        # its work, and every runner the tests start (.coveragerc: patch =
        # subprocess); each writes its own file into the node's directory, a
        # declared output the coverage node combines.
        data = f"$(B)/coverage/{module}"
        env["COVERAGE_FILE"] = f"{data}/.coverage"
        inputs.append("$(S)/.coveragerc")
        prelude = [mkdir(data)]
        outputs.append(data)
        run = ["python3", "-m", "coverage", "run", "-m", "unittest", "-v", module]
    return command(
        name=module,
        inputs=inputs,
        outputs=outputs,
        cmd=[*prelude, run, touch(stamp)],
        cwd="$(S)",
        env=env,
        descr="UT",
        color="green",
    )


tests = [
    unit_test("test_build_system", ["$(S)/build"]),
    unit_test("test_style", ["$(S)/style.py"]),
]

group("test", *tests)

if COVERAGE:
    combined = "$(B)/coverage/.coverage"
    coverage = command(
        name="coverage",
        inputs=["$(S)/.coveragerc"],
        outputs=["$(B)/coverage.xml", combined],
        deps=tests,
        cmd=[
            # --keep: the inputs are the test nodes' restored outputs
            ["python3", "-m", "coverage", "combine", "--keep", f"--data-file={combined}",
             *(test.outputs[1] for test in tests)],
            ["python3", "-m", "coverage", "report", f"--data-file={combined}",
             f"--fail-under={COVERAGE_MINIMUM}"],
            ["python3", "-m", "coverage", "xml", f"--data-file={combined}", "-o", "$(B)/coverage.xml"],
        ],
        cwd="$(S)",
        env=PYTHON_ENV,
        descr="CV",
        color="magenta",
    )
