#!/usr/bin/env python3

import contextlib
import importlib.machinery
import importlib.util
import io
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
LOADER = importlib.machinery.SourceFileLoader("imway_build_runner", str(ROOT / "build"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
LOADER.exec_module(runner)


class BuildSystemTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / "out"

    def tearDown(self):
        self.temp.cleanup()

    def context(self):
        return runner.BuildContext(self.root, self.out)

    def test_default_build_root_and_executor_layout(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            args = runner.parse_args([])
            self.assertEqual(args.build_dir, ".build")
            self.assertFalse(args.ninja)
        self.assertTrue(runner.parse_args(["--ninja"]).ninja)
        self.assertTrue(runner.parse_args(["-T"]).ninja)
        executor = runner.Executor(self.context(), 1, False, False)
        self.assertEqual(executor.cas, self.out / "cas")
        self.assertEqual(executor.uids, self.out / "uid")
        self.assertEqual(executor.tmp, self.out / "tmp")
        self.assertEqual(executor.grb, self.out / "grb")

    def test_global_flags_default_to_parsed_environment(self):
        environment = {
            "CFLAGS": "-gc '-DNAME=two words'",
            "CXXFLAGS": "-gx",
            "CPPFLAGS": "-gp",
            "LDFLAGS": "-gl",
            "CTRFLAGS": "-ctr one",
        }
        with mock.patch.dict("os.environ", environment, clear=True):
            context = self.context()
        self.assertEqual(context.cflags, ["-gc", "-DNAME=two words"])
        self.assertEqual(context.cxxflags, ["-gx"])
        self.assertEqual(context.cppflags, ["-gp"])
        self.assertEqual(context.ldflags, ["-gl", "-ctr", "one"])

    def test_rejects_unrooted_graph_paths(self):
        context = self.context()
        with self.assertRaisesRegex(runner.BuildError, r"input path must start with \$\(S\)/"):
            context.program(name="app", srcs=["main.c"])
        with self.assertRaisesRegex(runner.BuildError, r"output path must start with \$\(B\)/"):
            context.command(name="generated", outputs=["generated.h"], cmd=[["true"]])
        with self.assertRaisesRegex(runner.BuildError, r"cwd path must start with \$\(S\)/"):
            context.command(
                name="generated", outputs=["$(B)/generated.h"], cmd=[["true"]], cwd="tools",
            )

        (self.root / "build.py").write_text(
            "import build\n"
            "build.includes += ['include']\n"
            "app = program(srcs=['$(S)/main.c'])\n",
        )
        with self.assertRaisesRegex(runner.BuildError, r"include path must start with \$\(S\)/"):
            context.load(self.root / "build.py")

    def test_build_glob_returns_sorted_symbolic_paths(self):
        (self.root / "z.cpp").write_text("\n")
        (self.root / "a.cpp").write_text("\n")
        (self.root / "ignored.c").write_text("\n")
        context = self.context()
        self.assertEqual(
            context.glob("$(S)/*.cpp"),
            ["$(S)/a.cpp", "$(S)/z.cpp"],
        )
        with self.assertRaisesRegex(runner.BuildError, r"glob pattern must start with \$\(S\)/"):
            context.glob("*.cpp")

    def test_infers_target_name_from_module_global(self):
        (self.root / "build.py").write_text(
            "thing = library(srcs=['$(S)/thing.c'])\ninstall(thing)\n",
        )
        (self.root / "thing.c").write_text("int thing;\n")
        context = self.context()
        context.load(self.root / "build.py")
        context.build_graph()
        self.assertEqual(context.target_names["thing"].root.outputs, ["$(B)/libthing.a"])

    def test_build_module_defines_global_includes(self):
        (self.root / "main.c").write_text("int main(void) { return 0; }\n")
        (self.root / "lib.c").write_text("int value;\n")
        (self.root / "build.py").write_text(
            "import build\n"
            "build.includes += ['$(S)/include', '$(B)/generated']\n"
            "build.cflags += ['-project-c']\n"
            "build.cxxflags += ['-project-cxx']\n"
            "build.cppflags += ['-project-cpp']\n"
            "build.ldflags += ['-project-ld']\n"
            "app = program(srcs=build.glob('$(S)/main.c'))\n"
            "lib = library(srcs=['$(S)/lib.c'])\n",
        )
        context = self.context()
        context.load(self.root / "build.py")
        context.build_graph()
        self.assertEqual(context.includes, ["$(S)/include", "$(B)/generated"])
        self.assertEqual(context.cflags[-1], "-project-c")
        self.assertEqual(context.cxxflags[-1], "-project-cxx")
        self.assertEqual(context.cppflags[-1], "-project-cpp")
        self.assertEqual(context.ldflags[-1], "-project-ld")
        for name in ("app", "lib"):
            command = context.target_names[name].nodes[0].commands[0]
            self.assertIn("-I$(S)", command)
            self.assertIn("-I$(S)/include", command)
            self.assertIn("-I$(B)/generated", command)

    def test_target_flags_follow_global_and_dependency_flags(self):
        (self.root / "main.c").write_text("int c;\n")
        (self.root / "main.cpp").write_text("int cxx;\n")
        context = self.context()
        context.cppflags = ["-global-cpp"]
        context.cflags = ["-global-c"]
        context.cxxflags = ["-global-cxx"]
        context.ldflags = ["-global-ld"]
        dependency = context.interface(
            cppflags=["-dep-cpp"], cflags=["-dep-c"], cxxflags=["-dep-cxx"],
            ldflags=["-dep-ld"],
        )
        app = context.program(
            name="app", srcs=["$(S)/main.c", "$(S)/main.cpp"], deps=[dependency],
            cppflags=["-local-cpp"], cflags=["-local-c"], cxxflags=["-local-cxx"],
            ldflags=["-local-ld"],
        )
        context.build_graph()

        c_command = app.nodes[0].commands[0]
        cxx_command = app.nodes[1].commands[0]
        self.assertEqual(
            c_command[1:c_command.index("-c")],
            ["-I$(S)", "-global-cpp", "-global-c", "-dep-cpp", "-dep-c", "-local-cpp", "-local-c"],
        )
        self.assertEqual(
            cxx_command[1:cxx_command.index("-c")],
            [
                "-I$(S)", "-global-cpp", "-global-c", "-global-cxx",
                "-dep-cpp", "-dep-c", "-dep-cxx",
                "-local-cpp", "-local-c", "-local-cxx",
            ],
        )
        self.assertEqual(app.root.commands[0][-3:], ["-global-ld", "-dep-ld", "-local-ld"])

    def test_project_includes_precede_environment_dependencies(self):
        (self.root / "main.cpp").write_text("int main() {}\n")
        context = self.context()
        context.cppflags = ["-I/external/dependency"]
        app = context.program(name="app", srcs=["$(S)/main.cpp"])
        context.build_graph()

        command = app.nodes[0].commands[0]
        self.assertLess(command.index("-I$(S)"),
                        command.index("-I/external/dependency"))

    def test_node_descriptions_and_colors(self):
        (self.root / "thing.cpp").write_text("int thing;\n")
        context = self.context()
        library = context.library(name="thing", srcs=["$(S)/thing.cpp"])
        program = context.program(name="app", srcs=["$(S)/thing.cpp"])
        generated = context.command(
            name="generated", outputs=["$(B)/generated.h"],
            cmd=[[sys.executable, "-c", "pass"]], descr="PB", color="light-cyan",
        )
        context.build_graph()

        self.assertEqual((library.nodes[0].descr, library.nodes[0].color), ("CC", "green"))
        self.assertEqual((library.root.descr, library.root.color), ("AR", "light-red"))
        self.assertEqual((program.root.descr, program.root.color), ("LD", "light-blue"))
        self.assertEqual((generated.root.descr, generated.root.color), ("PB", "light-cyan"))

    def test_scans_transitive_project_and_generated_includes(self):
        (self.root / "main.cpp").write_text(
            '#include <api/public.h>\n#include <generated.h>\nint main() {}\n',
        )
        (self.root / "inc/api").mkdir(parents=True)
        (self.root / "inc/api/public.h").write_text('#include "detail.h"\n')
        (self.root / "inc/api/detail.h").write_text("// detail\n")

        context = self.context()
        context.includes = ["$(S)/inc", "$(B)/gen"]
        app = context.program(name="app", srcs=["$(S)/main.cpp"])
        generated = context.command(
            name="generated", outputs=["$(B)/gen/generated.h"],
            cmd=[[sys.executable, "-c", "pass"]],
        )
        context.build_graph()
        compile_node = app.nodes[0]
        self.assertEqual(
            compile_node.source_inputs,
            {
                "$(S)/main.cpp",
                "$(S)/inc/api/public.h",
                "$(S)/inc/api/detail.h",
            },
        )
        self.assertIn(generated.root, compile_node.deps)

    def test_header_content_changes_compile_uid(self):
        (self.root / "main.c").write_text('#include "value.h"\nint main(void) { return V; }\n')
        header = self.root / "value.h"
        header.write_text("#define V 1\n")

        first = self.context()
        app1 = first.program(name="app", srcs=["$(S)/main.c"])
        first.install(app1)
        first.build_graph()
        first.calculate_uids([app1.root])
        uid1 = app1.nodes[0].uid

        header.write_text("#define V 2\n")
        second = self.context()
        app2 = second.program(name="app", srcs=["$(S)/main.c"])
        second.install(app2)
        second.build_graph()
        second.calculate_uids([app2.root])
        self.assertNotEqual(uid1, app2.nodes[0].uid)

    def test_cyclic_headers_have_finite_closure(self):
        (self.root / "main.c").write_text('#include "a.h"\n')
        (self.root / "a.h").write_text('#include "b.h"\n')
        (self.root / "b.h").write_text('#include "a.h"\n')
        context = self.context()
        app = context.program(name="app", srcs=["$(S)/main.c"])
        context.build_graph()
        self.assertEqual(
            app.nodes[0].source_inputs,
            {"$(S)/main.c", "$(S)/a.h", "$(S)/b.h"},
        )

    def test_executor_reuses_uid_manifest_and_cas(self):
        source = self.root / "input.txt"
        count = self.root / "count.txt"
        source.write_text("payload\n")
        script = (
            "from pathlib import Path; import sys; "
            "src, out, count = map(Path, sys.argv[1:]); "
            "count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1'); "
            "out.write_text(src.read_text())"
        )

        def graph():
            context = self.context()
            target = context.command(
                name="copy", inputs=["$(S)/input.txt"], outputs=["$(B)/result.txt"],
                cmd=[[sys.executable, "-c", script, "$(S)/input.txt", "$(B)/result.txt", "$(S)/count.txt"]],
            )
            context.install(target)
            context.build_graph()
            context.calculate_uids([target.root])
            return context, target

        first, target1 = graph()
        with contextlib.redirect_stderr(io.StringIO()):
            runner.Executor(first, 2, False, False).run([target1.root])
        self.assertEqual((self.out / "result.txt").read_text(), "payload\n")
        self.assertEqual(count.read_text(), "1")

        second, target2 = graph()
        cached_stderr = io.StringIO()
        with contextlib.redirect_stderr(cached_stderr):
            runner.Executor(second, 2, False, False).run([target2.root])
        self.assertEqual(count.read_text(), "1")
        self.assertEqual(cached_stderr.getvalue(), "")

    def test_executor_reports_cache_miss_progress(self):
        context = self.context()
        target = context.command(
            name="write", outputs=["$(B)/result.txt"], descr="ZZ", color="magenta",
            cmd=[[
                sys.executable, "-c",
                "from pathlib import Path; import sys; sys.stderr.write('warning'); Path(sys.argv[1]).write_text('ok')",
                "$(B)/result.txt",
            ]],
        )
        context.install(target)
        context.build_graph()
        context.calculate_uids([target.root])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            runner.Executor(context, 1, False, False).run([target.root])
        self.assertEqual(stderr.getvalue(), "warning\n[ZZ] {1/1} $(B)/result.txt\n")

    def test_executor_prints_make_style_progress_by_default(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        stderr = TtyBuffer()
        with contextlib.redirect_stderr(stderr):
            executor = runner.Executor(self.context(), 1, False, False)
            executor.progress_total = 2
            executor._progress(runner.Node([], ["$(B)/thing.o"], [], descr="CC", color="green"))
            executor._progress(runner.Node([], ["$(B)/app"], [], descr="LD", color="light-blue"))
            executor._finish_progress()
        self.assertEqual(
            stderr.getvalue(),
            "[\x1b[32mCC\x1b[0m] {1/2} $(B)/thing.o\n"
            "[\x1b[94mLD\x1b[0m] {2/2} $(B)/app\n",
        )

    def test_executor_repaints_progress_in_ninja_mode(self):
        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        stderr = TtyBuffer()
        with contextlib.redirect_stderr(stderr):
            executor = runner.Executor(self.context(), 1, False, False, ninja=True)
            executor.progress_total = 1
            executor._progress(runner.Node([], ["$(B)/thing.o"], [], descr="CC", color="green"))
            executor._finish_progress()
        self.assertEqual(
            stderr.getvalue(),
            "\x1b[2K\r[\x1b[32mCC\x1b[0m] {1/1} $(B)/thing.o\r\n",
        )

    def test_failed_node_does_not_publish_manifest(self):
        context = self.context()
        target = context.command(
            name="fail", outputs=["$(B)/missing"],
            cmd=[[sys.executable, "-c", "raise SystemExit(7)"]],
        )
        context.build_graph()
        context.calculate_uids([target.root])
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(runner.BuildError):
                runner.Executor(context, 1, False, False).run([target.root])
        self.assertFalse(runner.Executor(context, 1, False, False)._manifest_path(target.root.uid).exists())

    def test_cli_target_is_published_in_source_root(self):
        project = self.root / "project"
        project.mkdir()
        shutil.copy2(ROOT / "build", project / "build")
        (project / "build.py").write_text(
            "app = command(name='app', outputs=['$(B)/bin/app'], cmd=[\n"
            "    'python3', '-c',\n"
            "    \"from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ok')\",\n"
            "    '$(B)/bin/app',\n"
            "])\n"
            "install(app)\n",
        )

        subprocess.run(
            [str(project / "build"), "app"], cwd=project,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        published = project / "app"
        self.assertTrue(published.is_symlink())
        self.assertEqual(published.readlink(), Path(".build/bin/app"))
        self.assertEqual(published.read_text(), "ok")

        published.unlink()
        subprocess.run(
            [str(project / "build")], cwd=project,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertFalse(published.exists())

    def test_publish_refuses_to_replace_source_file(self):
        context = self.context()
        target = context.command(
            name="app", outputs=["$(B)/app"],
            cmd=[[sys.executable, "-c", "pass"]],
        )
        context.build_graph()
        source = self.root / "app"
        source.write_text("keep")

        with self.assertRaisesRegex(runner.BuildError, "refusing to replace non-symlink"):
            context.publish([target])
        self.assertEqual(source.read_text(), "keep")

    def test_parser_ignores_commented_directives(self):
        source = self.root / "source.c"
        source.write_text(
            '/* #include "hidden.h" */\n// #include "also-hidden.h"\n#include "seen.h"\n',
        )
        (self.root / "seen.h").write_text("\n")
        context = self.context()
        scanner = runner.IncludeScanner(context)
        self.assertEqual(scanner._parse("$(S)/source.c"), [(True, "seen.h")])

    def test_sigint_kills_worker_process_group_without_traceback(self):
        project = self.root / "project"
        project.mkdir()
        shutil.copy2(ROOT / "build", project / "build")
        (project / "build.py").write_text(
            "job = command(name='job', outputs=['$(B)/done'], cmd=[\n"
            "    'python3', '-c',\n"
            "    \"import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)\",\n"
            "    '$(S)/worker.pid',\n"
            "])\ninstall(job)\n",
        )
        process = subprocess.Popen(
            [str(project / "build"), "-B", "out"], cwd=project,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        pid_file = project / "worker.pid"

        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not pid_file.exists():
                if process.poll() is None:
                    process.terminate()
                _stdout, stderr = process.communicate(timeout=5)
                self.fail(f"worker command did not start (rc={process.returncode}): {stderr}")
            worker_pid = int(pid_file.read_text())
            process.send_signal(signal.SIGINT)
            _stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 128 + signal.SIGINT)
            self.assertNotIn("Traceback", stderr)

            deadline = time.monotonic() + 2
            while Path(f"/proc/{worker_pid}").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(Path(f"/proc/{worker_pid}").exists(), "worker survived SIGINT")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
