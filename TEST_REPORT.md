# Test report

This report lists the checks run on this clean-room reimplementation of Click
to confirm it behaves like the original. All numbers were measured on Linux
with Python 3.13.

## Method

1. The package was written first, without reading Click's code. That version
   is the first commit (`d1a3a1e`).
2. Only then was Click's own test suite fetched (the `tests/` directory of
   the upstream `main` branch, which targets Click 8.6, plus its pytest
   configuration). The upstream source was never downloaded.
3. The suite was run, failures were analysed from the tests' expectations,
   the code was fixed, and the suite was re-run until it was clean.

## Result of the compatibility suite

| Outcome | Count |
| --- | ---: |
| Passed | 2262 |
| Skipped (Windows-only tests) | 24 |
| Expected failure (marked `xfail` upstream) | 1 |
| Failed | 0 |
| Deselected (stress tests, off by default) | 31000 |

The same result was obtained running from the source tree and from a clean
virtual environment with the package installed. The commit tested is the one
published on GitHub.

### Progress during development

| Stage | Passed | Failed |
| --- | ---: | ---: |
| First full run (after adding the missing internals the tests import) | 2031 | 230 |
| After styling, pager, prompt and utility fixes | 2186 | 76 |
| After the test runner, version option and help fixes | 2222 | 40 |
| After exit handling, context resources and type fixes | 2255 | 7 |
| After the last fixes | 2262 | 0 |

## Results by test file

| Test file | Passed | Skipped | Expected failure |
| --- | ---: | ---: | ---: |
| `test_options.py` | 699 | | |
| `test_termui.py` | 259 | 23 | |
| `test_arguments.py` | 206 | | |
| `test_compat.py` | 175 | | |
| `test_basic.py` | 109 | | |
| `test_commands.py` | 103 | | |
| `test_deprecations.py` | 66 | | |
| `test_shell_completion.py` | 58 | | |
| `test_defaults.py` | 46 | | |
| `test_testing.py` | 44 | 1 | |
| `test_info_dict.py` | 38 | | |
| `test_formatting.py` | 37 | | |
| `test_context.py` | 30 | | |
| `test_stream_lifecycle.py` | 24 | | |
| `test_chain.py` | 15 | | 1 |
| `test_command_decorators.py` | 9 | | |
| `test_abort_interrupt.py` | 8 | | |
| `test_parser.py` | 7 | | |
| `test_custom_classes.py` | 6 | | |
| `test_normalization.py` | 3 | | |
| `test_imports.py` | 1 | | |
| `test_exceptions/` | 1 | | |
| `test_types/` (8 files) | 68 | | |
| `test_utils/` (13 files) | 250 | | |
| **Total** | **2262** | **24** | **1** |

### What the main files cover

- **Options and arguments** (`test_options`, `test_arguments`, `test_defaults`):
  declarations and naming rules, defaults and "unset" handling, flags and
  competing flags that share a name, environment variables, `multiple` and
  `nargs`, help text, deprecation labels.
- **Commands and context** (`test_commands`, `test_context`, `test_chain`,
  `test_basic`): groups, chained commands, context objects, resources that
  close with the context, parameter sources, the help option.
- **Terminal interaction** (`test_termui`, `test_utils`): prompts and
  confirmations, hidden input masking, styling, progress bars, the pager,
  the editor, `echo`, file helpers.
- **Help output and errors** (`test_formatting`, `test_info_dict`,
  `test_compat`): exact help layout, usage errors, ANSI-aware wrapping.
- **Shell completion** (`test_shell_completion`): Bash, Zsh, Fish and
  PowerShell scripts and results.
- **Test runner** (`test_testing`, `test_stream_lifecycle`,
  `test_abort_interrupt`): `CliRunner`, file descriptor capture, stream
  ownership, behaviour on repeated Ctrl-C.
- **Types** (`test_types/`): `Choice`, `File`, `Path`, ranges, conversion.
- **Compatibility** (`test_deprecations`, `test_imports`): deprecated names
  still work with warnings, and `import click` stays light.

## Stress tests

These are off by default upstream. They were run explicitly:

| Test group | Cases run | Passed | Failed |
| --- | ---: | ---: | ---: |
| Pager cleanup (`test_echo_via_pager.py`) | 1000 (all) | 1000 | 0 |
| Stream handling (`test_stream_lifecycle.py`) | 1500 of 30000 (every 20th) | 1500 | 0 |

The first sample found a real bug: the pager stress test failed in all 50
sampled cases because an internal module was not loaded on import. It was
fixed and all 1000 pager cases then passed. The remaining 28,500
stream-handling cases were not run.

## Other checks

- **Smoke test:** a small group with options, arguments and a subcommand was
  run through `CliRunner` for help pages, usage errors, unknown commands and
  invalid choices, and the output was inspected by hand.
- **Static check:** `pyflakes` over the package reports nothing except the
  intentional re-exports in `__init__.py`.
- **Packaging:** the package builds and installs into a fresh virtual
  environment, and the suite passes against the installed copy.

## Limits of this evidence

- The suite is the strongest check available, but it only confirms behaviour
  the tests look at. Behaviour outside the tests could still differ.
- The 24 skipped tests only run on Windows. The Windows-specific code (key
  reading, editor paths) has not been executed.
- The target is upstream's development branch (Click 8.6), not the released
  8.5.0. A few behaviours differ between those two.
- A side-by-side comparison with real Click 8.5.0 (same programs and inputs,
  diffing output and exit codes) has not been done.
