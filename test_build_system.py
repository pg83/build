#!/usr/bin/env python3

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
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
            self.assertIsNone(args.target)
            self.assertFalse(args.ninja)
        self.assertEqual(
            runner.parse_args(["--target", "aarch64-unknown-linux-gnu"]).target,
            "aarch64-unknown-linux-gnu",
        )
        self.assertTrue(runner.parse_args(["--ninja"]).ninja)
        self.assertTrue(runner.parse_args(["-T"]).ninja)
        context = self.context()
        self.assertEqual(context.target, context.host)
        executor = runner.Executor(context, 1, False, False)
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
            [
                f"--target={context.target}",
                "-I$(S)", "-global-cpp", "-global-c", "-dep-cpp", "-dep-c",
                "-local-cpp", "-local-c",
            ],
        )
        self.assertEqual(
            cxx_command[1:cxx_command.index("-c")],
            [
                f"--target={context.target}",
                "-I$(S)", "-global-cpp", "-global-c", "-global-cxx",
                "-dep-cpp", "-dep-c", "-dep-cxx",
                "-local-cpp", "-local-c", "-local-cxx",
            ],
        )
        self.assertEqual(app.root.commands[0][-3:], ["-global-ld", "-dep-ld", "-local-ld"])

    def test_dependency_compile_flags_are_calculated_once_per_language(self):
        sources = []
        for index in range(4):
            for extension in ("c", "cpp"):
                path = self.root / f"source-{index}.{extension}"
                path.write_text("int value;\n")
                sources.append(f"$(S)/{path.name}")

        context = self.context()
        dependency = context.interface(cflags=["-dep-c"], cxxflags=["-dep-cxx"])
        context.program(name="app", srcs=sources, deps=[dependency])
        with mock.patch.object(
            context,
            "_usage_compile_flags",
            wraps=context._usage_compile_flags,
        ) as usage_compile_flags:
            context.build_graph()

        self.assertEqual(
            usage_compile_flags.call_args_list,
            [
                mock.call([dependency], False),
                mock.call([dependency], True),
            ],
        )

    def test_clang_always_receives_target_and_gcc_cannot_cross_compile(self):
        (self.root / "main.c").write_text("int main(void) { return 0; }\n")
        cross = self.context()
        cross.target = "aarch64-unknown-linux-gnu"
        app = cross.program(name="app", srcs=["$(S)/main.c"])
        cross.build_graph()
        self.assertEqual(
            app.nodes[0].commands[0][1],
            "--target=aarch64-unknown-linux-gnu",
        )
        self.assertEqual(
            app.root.commands[0][1],
            "--target=aarch64-unknown-linux-gnu",
        )

        gcc = runner.BuildContext(
            self.root,
            self.out,
            target="aarch64-unknown-linux-gnu",
            host="x86_64-unknown-linux-gnu",
        )
        gcc.cc = "gcc"
        gcc.program(name="gcc_app", srcs=["$(S)/main.c"])
        with mock.patch.object(gcc, "_compiler_kind", return_value="gcc"):
            with self.assertRaisesRegex(
                runner.BuildError,
                "GCC cross-compilation is not supported",
            ):
                gcc.build_graph()

        native_gcc = runner.BuildContext(
            self.root,
            self.out,
            host="x86_64-unknown-linux-gnu",
            target="x86_64-unknown-linux-gnu",
        )
        native_gcc.cc = "gcc"
        native_app = native_gcc.program(
            name="native_gcc_app",
            srcs=["$(S)/main.c"],
        )
        with mock.patch.object(native_gcc, "_compiler_kind", return_value="gcc"):
            native_gcc.build_graph()
        self.assertEqual(native_app.nodes[0].commands[0][0], "gcc")
        self.assertNotIn("--target", native_app.nodes[0].commands[0])

    def test_cross_graph_imports_host_program_without_target_variant(self):
        project = self.root / "project"
        project.mkdir()
        (project / "support.c").write_text("int support(void) { return 0; }\n")
        (project / "tool.cpp").write_text("int main() { return 0; }\n")
        (project / "app.c").write_text("int main(void) { return 0; }\n")
        (project / "build.py").write_text(
            "import build\n"
            "build.flags.allow({'FLAVOR': {'default': 'host'}})\n"
            "support = library(\n"
            "    srcs=['$(S)/support.c'],\n"
            "    cflags=['-DFLAVOR=' + build.flags.FLAVOR],\n"
            ")\n"
            "tool = program(srcs=['$(S)/tool.cpp'], deps=[support])\n"
            "generated = command(\n"
            "    inputs=['$(B)/tool'],\n"
            "    outputs=['$(B)/generated'],\n"
            "    cmd=['$(B)/tool', '$(B)/generated'],\n"
            ")\n"
            "generated_again = command(\n"
            "    outputs=['$(B)/generated-again'],\n"
            "    cmd=['$(B)/tool', '$(B)/generated-again'],\n"
            ")\n"
            "app = program(srcs=['$(S)/app.c'], deps=[support, generated, generated_again])\n"
            "install(app, generated, generated_again)\n",
        )
        environment = {
            "HOST_CFLAGS": "-host-c",
            "HOST_CXXFLAGS": "-host-cxx",
            "HOST_CPPFLAGS": "-host-cpp",
        }
        with mock.patch.dict("os.environ", environment, clear=False):
            context = runner.BuildContext(
                project,
                self.out,
                runner.Flags({"FLAVOR": "target"}),
                target="aarch64-unknown-linux-gnu",
            )
            context.load(project / "build.py")
            with mock.patch.object(
                runner.subprocess,
                "run",
                wraps=subprocess.run,
            ) as run:
                context.build_graph()
                context.build_graph()
            self.assertEqual(run.call_count, 1)

        tool = context.target_names["tool"]
        generated = context.target_names["generated"]
        generated_again = context.target_names["generated_again"]
        support = context.target_names["support"]
        app = context.target_names["app"]
        self.assertIn(tool.root, generated.root.deps)
        self.assertIn(tool.root, generated_again.root.deps)

        tool_compiles = [
            node for node in context.nodes
            if node.outputs and node.outputs[0].startswith("$(B)/obj/tool/")
        ]
        self.assertEqual(len(tool_compiles), 1)
        tool_command = tool_compiles[0].commands[0]
        self.assertIn(f"--target={context.host}", tool_command)
        self.assertNotIn("--target=aarch64-unknown-linux-gnu", tool_command)
        self.assertIn("-host-c", tool_command)
        self.assertIn("-host-cxx", tool_command)
        self.assertIn("-host-cpp", tool_command)

        support_compiles = [
            node for node in context.nodes
            if node.outputs and node.outputs[0].startswith("$(B)/obj/support/")
        ]
        self.assertEqual(len(support_compiles), 2)
        commands = [node.commands[0] for node in support_compiles]
        self.assertTrue(any("-DFLAVOR=host" in command for command in commands))
        self.assertTrue(any("-DFLAVOR=target" in command for command in commands))
        self.assertIs(context.output_nodes["$(B)/libsupport.a"], support.root)
        self.assertIn("--target=aarch64-unknown-linux-gnu", app.nodes[0].commands[0])

        graph = json.loads(context.serialize_graph())
        self.assertEqual(graph["version"], 2)
        support_archives = [
            record for record in graph["nodes"]
            if record["outputs"] == ["$(B)/libsupport.a"]
        ]
        self.assertEqual(len(support_archives), 2)
        decoded, _primary, exports = runner.BuildContext._decode_graph(
            graph,
            project / "build.py",
        )
        decoded_app, kind = runner.BuildContext._select_imported_root(
            decoded,
            exports,
            "$(B)/app",
            exact=True,
            target_name="app",
        )
        self.assertEqual(kind, "program")
        decoded_closure = runner.BuildContext._node_closure([decoded_app])
        self.assertEqual(
            len([
                node for node in decoded_closure
                if node.outputs == ["$(B)/libsupport.a"]
            ]),
            2,
        )

    def test_cross_host_program_executes_inside_custom_command(self):
        project = self.root / "execute-host"
        project.mkdir()
        (project / "tool.c").write_text(
            "int main(void) { return 0; }\n",
        )
        (project / "build.py").write_text(
            "tool = program(srcs=['$(S)/tool.c'])\n"
            "generated = command(\n"
            "    outputs=['$(B)/generated'],\n"
            "    cmd=['python3', '-c',\n"
            "         \"from pathlib import Path; import sys; assert Path(sys.argv[1]).is_file(); Path(sys.argv[2]).write_text('host-ok')\",\n"
            "         '$(B)/tool', '$(B)/generated'],\n"
            ")\n"
            "install(generated)\n",
        )
        context = runner.BuildContext(
            project,
            self.out,
            target="aarch64-unknown-linux-gnu",
        )
        context.load(project / "build.py")
        context.build_graph()
        generated = context.target_names["generated"]
        context.calculate_uids([generated.root])
        with contextlib.redirect_stderr(io.StringIO()):
            runner.Executor(context, 2, False, False).run([generated.root])
        self.assertEqual((self.out / "generated").read_text(), "host-ok")

    def test_project_includes_precede_environment_dependencies(self):
        (self.root / "main.cpp").write_text("int main() {}\n")
        context = self.context()
        context.cppflags = ["-I/external/dependency"]
        app = context.program(name="app", srcs=["$(S)/main.cpp"])
        context.build_graph()

        command = app.nodes[0].commands[0]
        self.assertLess(command.index("-I$(S)"),
                        command.index("-I/external/dependency"))

    def test_import_build_inherits_target_and_language_specific_extra_flags(self):
        project = self.root / "project"
        child = project / "child"
        child.mkdir(parents=True)
        (project / "main.c").write_text("int main(void) { return 0; }\n")
        (child / "child.cpp").write_text("int child() { return 0; }\n")
        (child / "build.py").write_text(
            "import build\n"
            "build.flags.allow({'MODE': {'default': 'child'}})\n"
            "child = library(\n"
            "    srcs=['$(S)/child.cpp'],\n"
            "    cppflags=['-DMODE=' + build.flags.MODE],\n"
            ")\n",
        )
        (project / "build.py").write_text(
            "import build\n"
            "build.flags.allow({'MODE': {'default': 'parent'}})\n"
            "child = import_build(\n"
            "    'child/build.py',\n"
            "    'libchild.a',\n"
            "    extra_cflags=['-import-c'],\n"
            "    extra_cxxflags=['-import-cxx'],\n"
            "    extra_cppflags=['-import-cpp'],\n"
            ")\n"
            "app = program(srcs=['$(S)/main.c'], deps=[child])\n",
        )

        context = runner.BuildContext(
            project,
            self.out,
            runner.Flags({"MODE": "parent-cli"}),
            target="aarch64-unknown-linux-gnu",
        )
        context.load(project / "build.py")
        context.build_graph()
        imported = context.target_names["child"]
        compile_node = next(
            node for node in imported.nodes
            if node.outputs and "/obj/child/" in node.outputs[0]
        )
        command = compile_node.commands[0]
        self.assertIn("--target=aarch64-unknown-linux-gnu", command)
        self.assertIn("-import-c", command)
        self.assertIn("-import-cxx", command)
        self.assertIn("-import-cpp", command)
        self.assertIn("-DMODE=child", command)
        self.assertNotIn("-DMODE=parent-cli", command)

    def test_import_build_caches_one_graph_for_multiple_outputs(self):
        project = self.root / "multi-import"
        child = project / "child"
        child.mkdir(parents=True)
        (child / "one.c").write_text("int one;\n")
        (child / "two.c").write_text("int two;\n")
        (child / "build.py").write_text(
            "one = library(srcs=['$(S)/one.c'])\n"
            "two = library(srcs=['$(S)/two.c'])\n",
        )
        context = runner.BuildContext(project, self.out)
        with mock.patch.object(
            runner.subprocess,
            "run",
            wraps=subprocess.run,
        ) as run:
            one = context.import_build("child/build.py", "libone.a")
            two = context.import_build("child/build.py", "libtwo.a")
        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(one.root, two.root)
        self.assertEqual(
            {one.root.outputs[0], two.root.outputs[0]},
            {"$(B)/child/libone.a", "$(B)/child/libtwo.a"},
        )

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

    def test_node_description_is_exactly_two_ascii_letters(self):
        for descr in ("A", "ABC", "A1", "A ", "ÄB"):
            with self.subTest(descr=descr):
                with self.assertRaisesRegex(
                    runner.BuildError,
                    "descr must be exactly two ASCII letters",
                ):
                    runner.Node([], [], [], descr=descr)

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

    def test_source_mapping_adds_inputs_to_one_compile_node(self):
        (self.root / "main.cpp").write_text("int main() {}\n")
        (self.root / "other.cpp").write_text("int other;\n")
        context = self.context()
        generated = context.command(
            name="generated",
            outputs=["$(B)/generated.h"],
            cmd=[[sys.executable, "-c", "pass"]],
        )
        app = context.program(
            name="app",
            srcs=[
                {
                    "src": "$(S)/main.cpp",
                    "inputs": ["$(B)/generated.h"],
                },
                "$(S)/other.cpp",
            ],
        )
        context.build_graph()

        self.assertIn(generated.root, app.nodes[0].deps)
        self.assertNotIn(generated.root, app.nodes[1].deps)

    def test_include_roots_are_reverse_indexed_with_one_walk_each(self):
        (self.root / "src").mkdir()
        (self.root / "src/main.cpp").write_text(
            '#include "local.h"\n#include "../parent.h"\n'
            '#include <api/public.h>\n#include <missing.h>\n',
        )
        (self.root / "src/local.h").write_text("// local\n")
        (self.root / "parent.h").write_text("// parent\n")
        for name, marker in (("first", "first"), ("second", "second")):
            (self.root / name / "api").mkdir(parents=True)
            (self.root / name / "api/public.h").write_text(f"// {marker}\n")

        context = self.context()
        context.includes = ["$(S)/first", "$(S)/second"]
        app = context.program(name="app", srcs=["$(S)/src/main.cpp"])
        real_walk = runner.os.walk
        with mock.patch.object(runner.os, "walk", wraps=real_walk) as walk:
            context.build_graph()

        walked = [Path(call.args[0]) for call in walk.call_args_list]
        for root in (self.root, self.root / "first", self.root / "second"):
            self.assertEqual(walked.count(root), 1)
        self.assertEqual(
            app.nodes[0].source_inputs,
            {
                "$(S)/src/main.cpp",
                "$(S)/src/local.h",
                "$(S)/parent.h",
                "$(S)/first/api/public.h",
            },
        )

    def test_include_scanner_skips_hidden_files_and_directories(self):
        (self.root / "main.cpp").write_text(
            "#include <visible.h>\n"
            "#include <.build/cas/cached.h>\n"
            "#include <.git/private.h>\n"
            "#include <nested/.private.h>\n",
        )
        (self.root / "visible.h").write_text("// visible\n")
        (self.root / ".build/cas").mkdir(parents=True)
        (self.root / ".build/cas/cached.h").write_text("// cached\n")
        (self.root / ".git").mkdir()
        (self.root / ".git/private.h").write_text("// private\n")
        (self.root / "nested").mkdir()
        (self.root / "nested/.private.h").write_text("// private\n")

        context = self.context()
        app = context.program(name="app", srcs=["$(S)/main.cpp"])
        real_walk = runner.os.walk

        def guarded_walk(root):
            for directory, directories, filenames in real_walk(root):
                yield directory, directories, filenames
                if any(name.startswith(".") for name in directories):
                    raise AssertionError("hidden directory was not pruned")

        with mock.patch.object(runner.os, "walk", side_effect=guarded_walk):
            context.build_graph()

        self.assertEqual(
            app.nodes[0].source_inputs,
            {"$(S)/main.cpp", "$(S)/visible.h"},
        )

    def test_include_resolution_does_not_stat_each_candidate(self):
        (self.root / "main.cpp").write_text(
            "".join(f"#include <missing-{index}.h>\n" for index in range(100)),
        )
        context = self.context()
        context.includes = [f"$(S)/include-{index}" for index in range(20)]
        app = context.program(name="app", srcs=["$(S)/main.cpp"])

        with mock.patch.object(Path, "is_file", side_effect=AssertionError("per-include stat")):
            context.build_graph()
        self.assertEqual(app.nodes[0].source_inputs, {"$(S)/main.cpp"})

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

    def test_scans_transitive_headers_from_absolute_source(self):
        with tempfile.TemporaryDirectory() as external_name:
            external = Path(external_name)
            source = external / "main.cpp"
            public = external / "public.h"
            detail = external / "detail.h"
            source.write_text('#include "public.h"\nint main() {}\n')
            public.write_text('#include "detail.h"\n')
            detail.write_text("// detail\n")

            context = self.context()
            app = context.program(name="app", srcs=[str(source)])
            context.build_graph()
            self.assertEqual(
                app.nodes[0].source_inputs,
                {str(source), str(public), str(detail)},
            )

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

    def test_executor_starts_one_garbage_collector(self):
        executor = runner.Executor(self.context(), 1, False, False)
        with mock.patch.object(runner.threading, "Thread") as thread:
            executor._start_garbage_collector()
            executor._start_garbage_collector()

        thread.assert_called_once_with(
            target=executor._garbage_collector,
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_executor_garbage_collector_removes_whole_grb_directory(self):
        class StopCollector(Exception):
            pass

        executor = runner.Executor(self.context(), 1, False, False)

        def remove(*args, **kwargs):
            self.assertTrue(executor.garbage_lock.locked())

        with (
            mock.patch.object(runner.subprocess, "run", side_effect=remove) as run,
            mock.patch.object(
                runner.time,
                "sleep",
                side_effect=StopCollector,
            ) as sleep,
            self.assertRaises(StopCollector),
        ):
            executor._garbage_collector()

        run.assert_called_once_with(
            ["rm", "-rf", str(executor.grb)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        sleep.assert_called_once_with(1)

    def test_executor_discard_recreates_grb_after_collector_race(self):
        executor = runner.Executor(self.context(), 1, False, False)
        work = executor.tmp / "work"
        work.mkdir(parents=True)
        (work / "output").write_text("data")
        real_rename = Path.rename
        attempts = 0

        def racing_rename(path, destination):
            nonlocal attempts
            self.assertTrue(executor.garbage_lock.locked())
            attempts += 1
            if attempts == 1:
                shutil.rmtree(executor.grb)
            return real_rename(path, destination)

        with mock.patch.object(
            Path,
            "rename",
            autospec=True,
            side_effect=racing_rename,
        ):
            executor._discard_contents(work)

        self.assertEqual(attempts, 2)
        self.assertFalse(work.exists())
        self.assertEqual(len(list(executor.grb.iterdir())), 1)

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

    def test_groups_are_additive_cli_aliases(self):
        project = self.root / "project"
        project.mkdir()
        shutil.copy2(ROOT / "build", project / "build")
        (project / "build.py").write_text(
            "one = command(outputs=['$(B)/one'],\n"
            "    cmd=['python3', '-c',\n"
            "         \"from pathlib import Path; Path(r'$(B)/one').touch()\"])\n"
            "two = command(outputs=['$(B)/two'],\n"
            "    cmd=['python3', '-c',\n"
            "         \"from pathlib import Path; Path(r'$(B)/two').touch()\"])\n"
            "group('batch', one)\n"
            "group('batch', two)\n"
            "group('install', one)\n",
        )

        listed = subprocess.run(
            [str(project / "build"), "--list"], cwd=project,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(
            listed.stdout.splitlines(),
            ["batch", "install", "one", "two"],
        )

        subprocess.run(
            [str(project / "build"), "batch"], cwd=project,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertTrue((project / ".build" / "one").exists())
        self.assertTrue((project / ".build" / "two").exists())
        self.assertFalse((project / "one").exists())
        self.assertFalse((project / "two").exists())

    def test_install_group_is_default_and_install_is_compatible(self):
        context = self.context()
        first = context.command(
            name="first", outputs=["$(B)/first"], cmd=[["true"]],
        )
        second = context.command(
            name="second", outputs=["$(B)/second"], cmd=[["true"]],
        )

        context.group("install", first)
        context.install(second)

        self.assertEqual(context.groups["install"], [first, second])

    def test_group_name_cannot_conflict_with_target_name(self):
        (self.root / "build.py").write_text(
            "same = command(outputs=['$(B)/same'], cmd=['true'])\n"
            "group('same', same)\n",
        )
        context = self.context()

        with self.assertRaisesRegex(
            runner.BuildError,
            "group name conflicts with target name: same",
        ):
            context.load(self.root / "build.py")

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

    def test_build_py_reads_declared_flags(self):
        (self.root / "build.py").write_text(
            "import build\n"
            "build.flags.allow({\n"
            "    'VARIANT': {'descr': 'build variant', 'default': 'debug'},\n"
            "    'EXTRA': {'descr': 'unused'},\n"
            "})\n"
            "app = command(name='app', outputs=['$(B)/' + build.flags.VARIANT],\n"
            "              cmd=[['true']])\n",
        )
        context = runner.BuildContext(
            self.root, self.out, runner.Flags({"VARIANT": "release"}),
        )
        context.load(self.root / "build.py")
        self.assertEqual(context.target_names["app"].outputs, ["$(B)/release"])

    def test_uid_calculation_resolves_command_tools(self):
        context = self.context()
        target = context.command(name="touch", outputs=["$(B)/out"], cmd=[["true"]])
        context.build_graph()
        context.calculate_uids([target.root])
        resolved = target.root.commands[0][0]
        self.assertTrue(os.path.isabs(resolved))
        self.assertEqual(resolved, os.path.realpath(shutil.which("true")))

    def test_resolve_tool_rejects_missing_command(self):
        with self.assertRaisesRegex(runner.BuildError, "command not found in PATH"):
            self.context().resolve_tool("definitely-not-a-real-tool-4f9a1")

    def test_cli_help_lists_declared_flags(self):
        project = self.root / "project"
        project.mkdir()
        shutil.copy2(ROOT / "build", project / "build")
        (project / "build.py").write_text(
            "import build\n"
            "build.flags.allow({'LTO': {'descr': 'enable lto', 'default': 'no'}})\n"
            "app = command(name='app', outputs=['$(B)/app'], cmd=[['true']])\n"
            "install(app)\n",
        )
        result = subprocess.run(
            [str(project / "build"), "-h"], cwd=project,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertIn("Build flags", result.stdout)
        self.assertIn("LTO", result.stdout)
        self.assertIn("enable lto", result.stdout)
        self.assertIn("(default: no)", result.stdout)

    def test_cli_reports_missing_build_file_without_traceback(self):
        project = self.root / "project"
        project.mkdir()
        shutil.copy2(ROOT / "build", project / "build")

        result = subprocess.run(
            [str(project / "build")],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            result.stderr,
            f"build: build file does not exist: {project / 'build.py'}\n",
        )
        self.assertNotIn("Traceback", result.stderr)

    def test_parser_ignores_commented_directives(self):
        source = self.root / "source.c"
        source.write_text(
            '/* #include "hidden.h" */\n'
            '// #include "also-hidden.h"\n'
            'const char* text = "/* #include \\"literal.h\\" */";\n'
            "# /* comment */ include <first.h>\n"
            "# /* comment\n"
            " */ include <split-directive.h>\n"
            "/* comment\n"
            " * across lines\n"
            " */ #include \"second.h\"\n",
        )
        context = self.context()
        scanner = runner.IncludeScanner(context)
        self.assertEqual(
            scanner._parse("$(S)/source.c"),
            [(False, "first.h"), (True, "second.h")],
        )

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


class FlagsTest(unittest.TestCase):
    def test_parse_defines(self):
        self.assertEqual(
            runner.parse_defines(["A=B", "C", "D="]),
            {"A": "B", "C": "yes", "D": ""},
        )
        with self.assertRaisesRegex(runner.BuildError, "malformed -D flag"):
            runner.parse_defines(["=value"])

    def test_unset_flag_reads_as_empty_string(self):
        flags = runner.Flags({"NAME": "value"})
        self.assertEqual(flags.NAME, "value")
        self.assertEqual(flags["NAME"], "value")
        self.assertEqual(flags.MISSING, "")
        self.assertFalse(flags.MISSING)
        self.assertIn("NAME", flags)
        self.assertNotIn("MISSING", flags)

    def test_allow_rejects_unknown_flags_and_applies_defaults(self):
        flags = runner.Flags({"KNOWN": "yes"})
        flags.allow({
            "KNOWN": {"descr": "known flag"},
            "TUNED": {"descr": "tunable", "default": "3"},
        })
        self.assertEqual(flags.KNOWN, "yes")
        self.assertEqual(flags.TUNED, "3")
        strict = runner.Flags({"MYSTERY": "yes"})
        with self.assertRaisesRegex(runner.BuildError, "unknown -D flag"):
            strict.allow({"KNOWN": {}})

    def test_allow_raises_spec_in_help_mode(self):
        flags = runner.Flags({}, help_mode=True)
        with self.assertRaises(runner.AllowSpec) as caught:
            flags.allow({"OPT": {"descr": "option", "default": "off"}})
        self.assertEqual(
            caught.exception.spec,
            {"OPT": {"descr": "option", "default": "off"}},
        )


if __name__ == "__main__":
    unittest.main()
