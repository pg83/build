#!/usr/bin/env python3

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_PATTERNS = ("*.cpp", "*.h")
INITIALIZER_LIST = re.compile(r"^(?P<indent> +):(?=\s)")


def source_files(arguments):
    if arguments:
        return [Path(argument).resolve() for argument in arguments]

    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--", *SOURCE_PATTERNS], cwd=ROOT
    )
    return [ROOT / name for name in output.decode().split("\0") if name]


def format_sources(files):
    command = shlex.split(os.environ.get("CLANG_FORMAT", "clang-format"))
    subprocess.run(
        [*command, "-i", "--style=file", *map(str, files)],
        cwd=ROOT,
        check=True,
    )


def restore_constructor_braces(path):
    lines = path.read_text().splitlines(keepends=True)
    changed = False
    in_initializers = False
    body_indent = ""
    parens = 0
    brackets = 0
    braces = 0
    quote = None
    escaped = False
    block_comment = False

    for line_number, line in enumerate(lines):
        if not in_initializers:
            match = INITIALIZER_LIST.match(line)
            if match is None:
                continue

            indent = match.group("indent")
            previous_number = line_number - 1
            while previous_number >= 0 and not lines[previous_number].strip():
                previous_number -= 1
            if previous_number < 0:
                continue
            previous = lines[previous_number]
            previous_indent = len(previous) - len(previous.lstrip(" "))
            if previous_indent + 4 != len(indent) or not previous.rstrip().endswith(")"):
                continue
            body_indent = indent[:-4] if indent.endswith("    ") else ""
            in_initializers = True
            parens = brackets = braces = 0
            quote = None
            escaped = False
            block_comment = False

        column = 0
        while column < len(line):
            char = line[column]
            following = line[column + 1] if column + 1 < len(line) else ""

            if block_comment:
                if char == "*" and following == "/":
                    block_comment = False
                    column += 2
                    continue
                column += 1
                continue

            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                column += 1
                continue

            if char == "/" and following == "/":
                break
            if char == "/" and following == "*":
                block_comment = True
                column += 2
                continue
            if char == '"' or char == "'":
                quote = char
                column += 1
                continue

            if char == "(":
                parens += 1
            elif char == ")":
                parens -= 1
            elif char == "[":
                brackets += 1
            elif char == "]":
                brackets -= 1
            elif char == "{":
                previous = line[:column].rstrip()
                is_body = (
                    parens == 0
                    and brackets == 0
                    and braces == 0
                    and previous.endswith(")")
                    and not line[column + 1 :].strip()
                )
                if is_body:
                    newline = "\r\n" if line.endswith("\r\n") else "\n"
                    lines[line_number] = (
                        previous + newline + body_indent + "{" + newline
                    )
                    in_initializers = False
                    changed = True
                    break
                braces += 1
            elif char == "}":
                braces -= 1

            column += 1

    if changed:
        path.write_text("".join(lines))


def main():
    files = source_files(sys.argv[1:])
    if not files:
        return

    format_sources(files)
    for path in files:
        restore_constructor_braces(path)


if __name__ == "__main__":
    main()
