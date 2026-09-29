import build

# The runner tests itself through its own graph: one node per unittest
# module, with the module and the file it covers as inputs, so a green module
# stays cached until one of them changes and both modules run in parallel.

PYTHON_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


def touch(path):
    return [
        "python3",
        "-c",
        f"from pathlib import Path; p=Path(r'{path}'); p.parent.mkdir(parents=True, exist_ok=True); p.touch()",
    ]


def unit_test(module, covers):
    # A passed suite produces nothing but its verdict, so the stamp is the
    # node's only output.
    stamp = f"$(B)/tests/{module}.stamp"
    return command(
        name=module,
        inputs=[f"$(S)/{module}.py", *covers],
        outputs=[stamp],
        cmd=[
            ["python3", "-m", "unittest", "-v", module],
            touch(stamp),
        ],
        cwd="$(S)",
        env=PYTHON_ENV,
        descr="UT",
        color="green",
    )


test_build_system = unit_test("test_build_system", ["$(S)/build"])
test_style = unit_test("test_style", ["$(S)/style.py"])

group("test", test_build_system, test_style)
