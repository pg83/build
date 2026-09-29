# build

`build` is a small, stateless Python build runner for C and C++ projects. It is
distributed as one executable file: copy it into a repository, add a
`build.py`, and run `./build`.

The runner imports `build.py`, constructs a dependency graph, scans project
includes, calculates content-based node UIDs, and executes missing nodes in
parallel. Outputs are kept in a local content-addressed cache under `.build`.
There is no configure database: `build.py` and every `pkg-config` query are
evaluated on every invocation.

Requirements are Linux and Python 3.10 or newer. Compilers, archivers,
`pkg-config`, and generators used by a project must be available in the
environment.

## Getting started

Copy the executable to the source root:

```sh
cp /path/to/build/build ./build
chmod +x ./build
```

Create `build.py` next to it:

```python
import build

build.cflags += ["-O2", "-g"]
build.cxxflags += ["-std=c++23"]
build.cppflags += ["-DPROJECT_FEATURE=1"]
build.includes += ["$(S)/include", "$(B)/generated"]

threads = pkg_config("threads")

version_header = command(
    name="version_header",
    inputs=["$(S)/VERSION", "$(S)/tools/gen-version"],
    outputs=["$(B)/generated/project/version.h"],
    cmd=[
        "$(S)/tools/gen-version",
        "$(S)/VERSION",
        "$(B)/generated/project/version.h",
    ],
    descr="GN",
    color="magenta",
)

core = library(
    srcs=build.glob("$(S)/src/core/*.cpp"),
    public_cppflags=["-DPROJECT_CORE=1"],
)

app = program(
    srcs=["$(S)/src/main.cpp"],
    deps=[core, threads],
)

group("install", app)
```

If a source includes `<project/version.h>`, the include scanner finds the
matching `$(B)/generated/project/version.h` output and adds `version_header` as
a dependency automatically.

Run the default `install` group:

```sh
./build
```

Or request a named target or group explicitly:

```sh
./build app
```

An explicit CLI target is also published as a relative symlink in the source
root. In this example `./app` points to `.build/app`. A bare invocation
publishes every member of the default `install` group. Other explicitly
selected groups build their members without publishing them individually. The
runner replaces an existing symlink atomically but never replaces a regular
source file or directory; when one already has the target name, publication is
skipped and the artifact remains available below the build root. Add published
target names to the project's `.gitignore`.

## Paths

Build descriptions use symbolic roots instead of paths relative to the current
directory:

- `$(S)` is the source root containing `build` and `build.py`.
- `$(B)` is the build root, `.build` by default.
- Source inputs must be `$(S)/...`, `$(B)/...`, or absolute paths.
- Declared outputs must be below `$(B)`.
- Include paths and command working directories must use `$(S)`, `$(B)`, or
  an absolute path.

Always spell project paths explicitly, for example `$(S)/src/main.cpp`, not
`src/main.cpp`. This keeps node descriptions independent of the process working
directory and makes cache keys reproducible.

`build.glob(pattern)` accepts a `$(S)/...` or absolute glob and returns sorted
symbolic paths. It does not search the build tree.

## Global configuration

`build.py` receives a synthetic module named `build`. Its mutable configuration
lists apply to the whole project:

```python
import build

build.includes += ["$(S)/include", "$(B)/generated"]
build.cppflags += ["-D_FILE_OFFSET_BITS=64"]
build.cflags += ["-Wall"]
build.cxxflags += ["-std=c++23"]
build.ldflags += ["-pthread"]
```

There is one include search configuration for the entire build. Do not repeat
include roots on individual targets.

The flag lists initially expose shell-parsed environment values while
`build.py` is evaluated:

- `CPPFLAGS` initializes `build.cppflags`.
- `CFLAGS` initializes `build.cflags`.
- `CXXFLAGS` initializes `build.cxxflags`.
- `LDFLAGS` and `CTRFLAGS` initialize `build.ldflags`.

`CC`, `CXX`, `AR`, and `PKG_CONFIG` select the corresponding tools. Per-target
flags extend the global flags and are placed later on the command line.

`build.host` is the current platform triple and `build.target` is the requested
target triple. They are equal unless `--target` was passed or the graph was
loaded by a cross-compiling `import_build()`.

For compilation the order is project global preprocessor/language flags,
global include roots, public flags from dependencies, target-local flags, then
environment flags. For linking it is project global linker flags, dependency
linker flags, target-local flags, then environment flags. This makes ordinary
environment flags user overrides while keeping them visible to build files.
Sanitizer-selection flags supplied through `CFLAGS` or `CXXFLAGS` are also
forwarded to compiler-driver link commands.

## Build flags

`./build -DNAME=value` passes ad-hoc flags to `build.py`, readable as
`build.flags`:

```python
import build

build.flags.allow({
    "LTO": {"descr": "enable link-time optimization", "default": "no"},
    "SANITIZER": {"descr": "address, thread, or undefined"},
})

if build.flags.LTO == "yes":
    build.cflags += ["-flto"]
    build.ldflags += ["-flto"]
if build.flags.SANITIZER:
    build.cflags += [f"-fsanitize={build.flags.SANITIZER}"]
```

An unset flag reads as the empty string, so `if build.flags.X:` is a plain
truthiness test. A bare `-DNAME` sets the flag to `"yes"`.

`allow()` declares the accepted flags. A `-D` outside the declared set is an
error, and an unset declared flag reads as its default. `./build -h` loads
`build.py` and lists the declared flags with their descriptions and defaults.

## Cross-compilation

Pass a Clang-compatible target triple with `--target`:

```sh
./build --target aarch64-unknown-linux-gnu
```

Every generated Clang compile and link command contains
`--target=<build.target>`, including native builds. GCC commands receive no
target option; selecting GCC when `build.host != build.target` is a
configuration error.

A `program()` output used as an exact `$(B)` argument in a custom command's
`inputs` or `cmd` is a host tool. When the host and target configurations
differ, the runner loads the same `build.py` recursively for `build.host`,
imports the host program's transitive node closure, and uses that program
instead of producing a target variant. Host and target nodes retain the same
symbolic output paths; their isolated UID work directories keep the artifacts
separate.

The host configuration does not inherit CLI `-D` flags. Shell-parsed
`HOST_CPPFLAGS`, `HOST_CFLAGS`, and `HOST_CXXFLAGS` are appended to host
compiles and propagate unchanged through nested imports. Configuration
identity includes the target triple and `-D` values. Compiler flags already
participate in each node UID through its command line.

## Targets

Targets can have an explicit `name=`. Otherwise a non-interface target must be
assigned to exactly one public module global and its variable name becomes the
target name:

```python
codec = library(srcs=["$(S)/codec.cpp"])
tool = program(srcs=["$(S)/tool.cpp"], deps=[codec])
```

Use explicit names for targets created in loops or comprehensions.

### `program()`

```python
program(
    srcs,
    name=None,
    deps=(),
    cflags=(),
    cxxflags=(),
    cppflags=(),
    includes=(),
    public_cflags=(),
    public_cxxflags=(),
    public_cppflags=(),
    ldflags=(),
    output=None,
    linker=None,
)
```

Builds and links a program. The default output is `$(B)/<name>`. `output` can
override it with another `$(B)/...` path. The C++ linker is selected if the
target or one of its target dependencies contains C++ sources; `linker` can
override that choice.

An entry in `srcs` may be a mapping when one compile node needs additional
inputs:

```python
srcs=[
    {
        "src": "$(S)/parser.cpp",
        "inputs": ["$(B)/parser.rl.h"],
    },
]
```

A declared `$(B)` input is replaced with a dependency on its producer. Source
inputs are hashed directly. Other sources in the target do not inherit these
inputs. A generated source cannot be scanned for includes at graph time, but
its declared source-tree inputs are, so listing the headers it includes gives
the compile node their include closure.

`includes` adds target-local include roots. They follow the source root and
precede `build.includes` on the compile command line and in include scanning,
and they are not propagated to consumers.

### `library()`

```python
library(
    srcs,
    name=None,
    deps=(),
    cflags=(),
    cxxflags=(),
    cppflags=(),
    includes=(),
    public_cflags=(),
    public_cxxflags=(),
    public_cppflags=(),
    ldflags=(),
    output=None,
)
```

Builds a static archive, `$(B)/lib<name>.a` by default. A program links static
library dependencies in dependency order. `public_*flags` propagate to
consumers; ordinary compile flags affect only the target itself. `ldflags`
propagate through dependencies and also participate in the final program link.
`includes` behaves as for `program()`.

### `dependency()`

```python
dependency(
    cflags=(),
    cxxflags=(),
    cppflags=(),
    ldflags=(),
    enabled=True,
    name=None,
)
```

Creates an interface-only target. It has no commands or outputs and carries
usage requirements to consumers. This is useful for dependencies described
directly in `build.py`.

### `import_build()`

```python
import_build(
    path,
    output_name,
    extra_cflags=(),
    extra_cxxflags=(),
    extra_cppflags=(),
    deps=(),
    namespace=None,
)
```

Loads another source-tree `build.py`, imports the transitive closure of the
export whose filename is `output_name`, and returns it as a target. The child
graph inherits `build.target` and the optional language-specific extra flags,
but it does not inherit CLI `-D` flags. Extra flags join the child's ordinary
`CFLAGS`, `CXXFLAGS`, and `CPPFLAGS`, so its `build.py` can inspect them. Every
nested `import_build()` appends its own values to the environment it inherited;
flags therefore accumulate in import order.

`deps` is supported when the imported root is a program. Each dependency must
already have a built output; that output is added to the imported program's
dependency list and appended to its link command, followed by its transitive
`ldflags`. This is useful when an imported test program must link an archive
produced by the parent graph.

`namespace` gives the import its own build prefix under `$(B)` instead of the
child's source-relative directory, so the same `build.py` can be imported more
than once with different extra flags (a ThinLTO flavour of a library next to
the plain one, for example) without the two builds colliding.

### `pkg_config()`

```python
wayland = pkg_config("wayland-server")
graphics = pkg_config("egl", "xkbcommon")
audio = pkg_config("libpulse", required=False)
```

Runs `pkg-config --cflags` and `pkg-config --libs` immediately and returns an
interface dependency. Queries are deliberately not persisted between builds.
With `required=False`, a failed query returns a disabled dependency, which is
false in a Python condition and contributes no flags.

`pkg_config_variable(package, variable)` returns a single queried variable:

```python
protocol_root = pkg_config_variable("wayland-protocols", "pkgdatadir")
```

### `have_header()`

```python
if have_header("optional/api.h"):
    backend = dependency(ldflags=["-loptional"])
else:
    backend = dependency()
```

Preprocesses an include with the configured C++ compiler, target triple, and
environment-derived `CPPFLAGS`, `CFLAGS`, and `CXXFLAGS`. It returns false when
the compiler cannot include the header. The probe runs while `build.py` is
loaded, so it is intended for system or toolchain headers, not generated files.
### `command()`

```python
command(
    outputs,
    cmd,
    inputs=(),
    deps=(),
    name=None,
    cwd="$(B)",
    env=None,
    cflags=(),
    cxxflags=(),
    cppflags=(),
    ldflags=(),
    descr="GN",
    color="yellow",
    local=False,
)
```

Creates a custom build node. `cmd` is either one argv list or a list of argv
lists executed in order. Commands are never passed through a shell. Declare
every source file or tool that affects the result in `inputs`; every generated
path must be declared in `outputs`. `deps` adds target dependencies. `cwd` and
all `env` values may contain `$(S)` and `$(B)`.

The optional flag arguments are public usage requirements for consumers of the
command target. `descr` is the progress label and must contain exactly two
ASCII letters. `color` is one of `red`, `green`, `yellow`, `blue`, `magenta`,
`cyan`, `white`, or their `light-*` variants.

`local=True` pins the node to this machine under `--dist`: use it for nodes
that need the network, the real source tree, or anything else a remote worker
lacks.

### `group()`

```python
group("install", app)
group("test", unit_tests)
group("test", integration_tests)
```

Defines a CLI alias for a set of targets. Calling `group()` repeatedly with the
same name adds targets to the existing group. Group names are included in
`--list`. Selecting a group does not publish source-root symlinks for its
members.

The `install` group is selected when the CLI has no positional names.
`install(*targets)` remains available as shorthand for
`group("install", *targets)`.

## Include scanning and dependency inference

C and C++ sources below `$(S)`, and sources declared with absolute paths, are
scanned for quoted and angle-bracket `#include`, `#include_next` and `#import`
directives. Comments are ignored. Resolution is recursive and cached for the
duration of the build:

1. A quoted include is tried relative to the including file.
2. The source root, the target's `includes` and every entry in
   `build.includes` are searched in that order.
3. A matching source file becomes a hashed source input.
4. A matching declared `$(B)` output adds its producer node as a dependency.
5. An unresolved include is treated as a system header and ignored.

`#include_next` skips the includer's own directory and continues the search
from the include root after the one that supplied the including file, as the
preprocessor does.

Changing any transitively included project header therefore changes the
compile node UID. Generated headers do not need to be repeated in a manual
`deps=` list when their declared output is reachable through the global include
roots.

## Cache and execution model

Each node UID is MD5 over its canonical command description, dependency UIDs,
and the names and MD5 hashes of source inputs. A command tool named without a
path is resolved through `PATH` to its absolute selected path before UID
calculation. The final symlink is preserved because multicall programs use
`argv[0]` to select their behavior; content-addressed PATH realms already carry
the toolchain identity in that path. Nodes run in parallel once their
dependencies are ready. Produced files are stored in a SHA-256-addressed CAS
and restored into the build root as symlinks. A failed command never publishes
a manifest.

Every command receives `BUILD_JOBS` with the runner's job count and `TMPDIR`
pointing at its private UID work directory.

The runner intentionally has no persistent configuration state. Deleting
`.build`, or passing `--clear`, discards reusable build artifacts without
changing the graph definition.

Commands inherit stdout. Their stderr is captured so concurrent diagnostics do
not interleave, then emitted as one block. Interrupting the build kills its
worker process group, including subprocesses started by commands.

### Undeclared-input audit

On Linux, `--strace` reruns every selected node under `strace`, including cache
hits, and rejects successful reads from the source tree that are not declared
by the node or its transitive dependency closure. Generated files, build roots,
`.git`, failed probes, and files created by the command itself are ignored.
This mode requires `strace` on `PATH` and Python's `sqlite3` module (which
backs the trace input index; an interpreter without it still runs every
untraced build), and is intended for CI or periodic build graph audits rather
than ordinary incremental builds. `--strace` and `--dist` are mutually
exclusive.

### Distributed builds

```
./build --dist=lab1,lab2 --dist=lab3:32 test
```

`--dist` adds ssh hosts as workers. Each host gets one ssh session; the engine
sends itself over as `build exec` and then streams jobs to it. A job carries
its command, environment and the manifests of its declared inputs; the host
keeps a per-session content-addressed cache under `.build-exec` in its
`TMPDIR` (the login directory when there is none), so every blob crosses the
link at most once. Results land in the local CAS exactly as if the node had
run here. Dropped sessions reconnect with backoff, and in-flight nodes go back
to the queue. `HOST:N` overrides the slot count (default: the host's cores).

Jobs run with the controller's `CC`, `CXX`, `AR` and `PATH` prepended to the
host's own, and tool names stay unresolved in node UIDs, so every host looks
the toolchain up itself; the same toolchain paths are assumed, not checked.
A node that must stay on this machine is declared with `local=True`. A remote
host only sees a node's declared inputs, so `--dist` also acts as a
hermeticity gate. Set `BUILD_DIST_SSH` to use another ssh command.

## CLI

```text
./build [options] [targets...]

  -B, --build-dir DIR   build root; default .build or environment variable B
      --cache-dir DIR   shared CAS/UID root; default build root or BUILD_CACHE_DIR
      --target TRIPLE   target triple; default is the current host platform
  -j, --jobs N          parallel worker count; default CPU count
  -D KEY[=VALUE]        build flag readable in build.py as build.flags.KEY
  -h, --help            show usage and the build's declared -D flags
  -k, --keep-going      continue independent work after a failed node
  -v, --verbose         print cache hits and command starts
  -T, --ninja           repaint one progress line on a terminal
      --strace          rerun nodes and reject undeclared source reads
      --dist HOST[:N],... also run nodes on these ssh hosts (repeatable)
      --clear           clear CAS, UID, temporary, and garbage directories
      --list            list named targets and groups without building
```

`--cache-dir` separates immutable cache state from the materialized build
root. This allows concurrent checkouts to use distinct `-B` directories while
sharing node manifests and CAS objects. UID-scoped work locks prevent duplicate
execution of the same node across those processes.

Default progress output keeps one line per completed node. `--ninja` uses an
in-place progress line on a terminal and falls back to normal lines when stderr
is redirected.

## Upstream development

The runner tests itself through its own graph. `build.py` declares one node
per test module, with the module and the file it covers as inputs, so

```sh
./build test
```

runs both modules in parallel and keeps a green module cached until it or the
code it covers changes. `./build --strace test` audits the test nodes' declared
inputs like any project's nodes. `python3 -m unittest` still runs the suite
directly.

With [coverage.py](https://coverage.readthedocs.io/) 7.10 or later on `PATH`,

```sh
./build -Dcoverage coverage
```

measures `build` and `style.py` across the test process, the child `build`
forks to do its work and every runner the tests start (`.coveragerc`), prints
the line report, fails below the floor set in `build.py`, and writes
`.build/coverage.xml`. CI (`.github/workflows/ci.yml`) runs `./build test` on
Linux and macOS for every push and pull request, and uploads the Linux
coverage to Codecov.

The `build` file is the distributable artifact. Projects should update their
vendored copy from this repository without modifying it locally.
