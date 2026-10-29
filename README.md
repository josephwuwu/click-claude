# click (clean-room reimplementation)

A from-scratch implementation of the [Click](https://github.com/pallets/click)
command line toolkit's public API. It installs as the `click` package and is
meant to be a drop-in replacement: commands, groups, options, arguments,
parameter types, help formatting, prompts, styling, progress bars, paging,
editing, shell completion (Bash, Zsh, Fish, PowerShell) and the `CliRunner`
test helper.

No code from the original project was copied. The modules were written from
a description of Click's documented behavior; the upstream test suite was
used afterwards only to check that the behavior matches.

## Usage

```python
import click

@click.group()
@click.option("--debug/--no-debug", default=False)
@click.pass_context
def cli(ctx, debug):
    """A demo CLI."""
    ctx.obj = {"debug": debug}

@cli.command()
@click.option("-n", "--count", default=1, show_default=True)
@click.argument("name")
def hello(count, name):
    """Say hello to NAME."""
    for _ in range(count):
        click.echo(f"Hello {name}!")

if __name__ == "__main__":
    cli()
```

## Layout

| Module | Contents |
| --- | --- |
| `core.py` | `Context`, `Command`, `Group`, `CommandCollection`, `Parameter`, `Option`, `Argument` |
| `decorators.py` | `command`, `group`, `option`, `argument`, `pass_context`, version and help options |
| `parser.py` | Tokenizes the command line into option values and positional arguments |
| `types.py` | Parameter types: `Choice`, `IntRange`, `Path`, `File`, `DateTime`, `Tuple`, ... |
| `formatting.py`, `_textwrap.py` | Help page layout and ANSI-aware text wrapping |
| `termui.py`, `_termui_impl.py` | Prompts, `style`/`secho`, progress bar, pager, editor, `getchar` |
| `shell_completion.py` | Completion protocol and shell scripts |
| `testing.py` | `CliRunner` with `sys` or file descriptor capture |
| `utils.py`, `_compat.py`, `globals.py`, `exceptions.py`, `_utils.py` | Support code |

Importing `click` only loads a small set of standard library modules; the
heavier ones are imported when first needed.

## Compatibility testing

The behavior is checked against the upstream test suite (main branch, which
targets Click 8.6). Only the `tests/` directory is fetched, never the
upstream source:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/pallets/click.git upstream-tests
```

```bash
git -C upstream-tests sparse-checkout set --no-cone '/tests/'
```

```bash
cp pyproject.toml upstream-tests/
```

```bash
cd upstream-tests && PYTHONPATH=../src python -m pytest tests
```

Result: all 2262 tests pass. 24 are skipped because they only run on
Windows, and 1 is marked as an expected failure upstream. The stress tests
(`-m stress`) are deselected by default, as upstream does; they pass when run
explicitly (all 1000 pager cases and a 1 in 20 sample of the 30000 stream
lifecycle cases were run).
