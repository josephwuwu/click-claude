"""Shell completion support for Bash, Zsh and Fish.

The shell runs the program with an environment variable such as
``_PROG_COMPLETE=bash_complete`` plus the words typed so far. The program
then works out which command, option or argument is being completed and
prints the candidates in a format the shell script understands.
"""

from __future__ import annotations

import collections.abc as cabc
import os
import re
import typing as t
from gettext import gettext as _

from .core import Argument
from .core import Command
from .core import Context
from .core import Group
from .core import Option
from .core import Parameter
from .core import ParameterSource
from .utils import echo


def shell_complete(
    cli: Command,
    ctx_args: cabc.MutableMapping[str, t.Any],
    prog_name: str,
    complete_var: str,
    instruction: str,
) -> int:
    """Handle a completion instruction like ``bash_source`` or
    ``zsh_complete``. Returns the exit code.
    """
    shell, _, action = instruction.partition("_")
    comp_cls = get_completion_class(shell)
    if comp_cls is None:
        return 1

    comp = comp_cls(cli, ctx_args, prog_name, complete_var)
    if action == "source":
        # Written as bytes so that the script keeps LF line endings even
        # where the text stream would translate them.
        echo(comp.source().encode("utf-8"))
        return 0
    if action == "complete":
        echo(comp.complete())
        return 0
    return 1


class CompletionItem:
    """One completion candidate.

    :param value: the text to complete.
    :param type: ``"plain"``, or ``"file"``/``"dir"`` to let the shell
        complete paths itself.
    :param help: description, for shells that show one.
    :param kwargs: extra data available as attributes.
    """

    __slots__ = ("value", "type", "help", "_info")

    def __init__(
        self,
        value: t.Any,
        type: str = "plain",
        help: str | None = None,
        **kwargs: t.Any,
    ) -> None:
        self.value: t.Any = value
        self.type: str = type
        self.help: str | None = help
        self._info = kwargs

    def __getattr__(self, name: str) -> t.Any:
        return self._info.get(name)


_BASH_SOURCE = """\
%(complete_func)s() {
    local IFS=$'\\n'
    local response
    response=$(env COMP_WORDS="${COMP_WORDS[*]}" COMP_CWORD=$COMP_CWORD \
%(complete_var)s=bash_complete $1)

    for completion in $response; do
        IFS=',' read kind value <<< "$completion"
        case $kind in
            dir)
                COMPREPLY=()
                compopt -o dirnames
                ;;
            file)
                COMPREPLY=()
                compopt -o default
                ;;
            plain)
                COMPREPLY+=("$value")
                ;;
        esac
    done
    return 0
}

%(complete_func)s_setup() {
    complete -o nosort -F %(complete_func)s %(prog_name)s
}

%(complete_func)s_setup;
"""

_ZSH_SOURCE = """\
#compdef %(prog_name)s

%(complete_func)s() {
    local -a plain_items
    local -a described_items
    local -a response
    (( ! $+commands[%(prog_name)s] )) && return 1

    response=("${(@f)$(env COMP_WORDS="${words[*]}" COMP_CWORD=$((CURRENT-1)) \
%(complete_var)s=zsh_complete %(prog_name)s)}")

    for kind key descr in ${response}; do
        case "$kind" in
            plain)
                if [[ "$descr" == "_" ]]; then
                    plain_items+=("$key")
                else
                    described_items+=("$key":"$descr")
                fi
                ;;
            dir)
                _path_files -/
                ;;
            file)
                _path_files -f
                ;;
        esac
    done

    if [ -n "$described_items" ]; then
        _describe -V unsorted described_items -U
    fi

    if [ -n "$plain_items" ]; then
        compadd -U -V unsorted -a plain_items
    fi
}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    # Loaded through fpath autoloading.
    %(complete_func)s "$@"
else
    # Loaded by sourcing the script.
    compdef %(complete_func)s %(prog_name)s
fi
"""

_FISH_SOURCE = """\
function %(complete_func)s
    set -l response (env %(complete_var)s=fish_complete COMP_WORDS=(commandline -cp) \
COMP_CWORD=(commandline -t) %(prog_name)s)

    for completion in $response
        set -l metadata (string split "," -- $completion)

        switch $metadata[1]
            case dir
                __fish_complete_directories $metadata[2]
            case file
                __fish_complete_path $metadata[2]
            case plain
                echo $metadata[2]
        end
    end
end

complete --no-files --command %(prog_name)s --arguments \
"(%(complete_func)s)"
"""


_POWERSHELL_SOURCE = """\
Register-ArgumentCompleter -Native -CommandName %(prog_name)s -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $line = $commandAst.ToString()
    if ($cursorPosition -lt $line.Length) {
        $line = $line.Substring(0, $cursorPosition)
    }
    $before = @($commandAst.CommandElements | Where-Object {
        $_.Extent.StartOffset -lt $cursorPosition
    })
    $index = $before.Count
    if ($wordToComplete -ne '') {
        $index -= 1
    }

    $env:COMP_WORDS = $line
    $env:COMP_CWORD = $index
    $env:%(complete_var)s = 'powershell_complete'
    try {
        $response = @(& %(prog_name)s)
    } finally {
        Remove-Item Env:COMP_WORDS, Env:COMP_CWORD, Env:%(complete_var)s
    }

    for ($i = 0; $i + 2 -lt $response.Count; $i += 3) {
        $kind = $response[$i]
        $value = $response[$i + 1]
        $description = $response[$i + 2]
        if ($description -eq '_') {
            $description = $value
        }

        switch ($kind) {
            'plain' {
                [System.Management.Automation.CompletionResult]::new(
                    $value, $value, 'ParameterValue', $description)
            }
            'dir' {
                Get-ChildItem -Directory -Path "$value*" | ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_.FullName, $_.Name, 'ProviderContainer', $_.Name)
                }
            }
            'file' {
                Get-ChildItem -Path "$value*" | ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new(
                        $_.FullName, $_.Name, 'ProviderItem', $_.Name)
                }
            }
        }
    }
}
"""


class ShellComplete:
    """Base class for shell integrations.

    Subclasses provide :attr:`name`, :attr:`source_template`,
    :meth:`get_completion_args` and :meth:`format_completion`.
    """

    name: t.ClassVar[str]
    source_template: t.ClassVar[str]

    def __init__(
        self,
        cli: Command,
        ctx_args: cabc.MutableMapping[str, t.Any],
        prog_name: str,
        complete_var: str,
    ) -> None:
        self.cli = cli
        self.ctx_args = ctx_args
        self.prog_name = prog_name
        self.complete_var = complete_var

    @property
    def func_name(self) -> str:
        """Name of the shell function defined by the script."""
        safe = re.sub(r"\W*", "", self.prog_name.replace("-", "_"), flags=re.ASCII)
        return f"_{safe}_completion"

    def source_vars(self) -> dict[str, t.Any]:
        """Values substituted into :attr:`source_template`."""
        return {
            "complete_func": self.func_name,
            "complete_var": self.complete_var,
            "prog_name": self.prog_name,
        }

    def source(self) -> str:
        """The script the user loads into their shell."""
        return self.source_template % self.source_vars()

    def get_completion_args(self) -> tuple[list[str], str]:
        """Return the complete words before the cursor and the word being
        completed, from the environment set by the script.
        """
        raise NotImplementedError

    def get_completions(self, args: list[str], incomplete: str) -> list[CompletionItem]:
        ctx = _resolve_context(self.cli, self.ctx_args, self.prog_name, args)
        obj, incomplete = _resolve_incomplete(ctx, args, incomplete)
        return obj.shell_complete(ctx, incomplete)

    def format_completion(self, item: CompletionItem) -> str:
        raise NotImplementedError

    def complete(self) -> str:
        args, incomplete = self.get_completion_args()
        items = self.get_completions(args, incomplete)
        return "\n".join(self.format_completion(item) for item in items)


class BashComplete(ShellComplete):
    name = "bash"
    source_template = _BASH_SOURCE

    @staticmethod
    def _check_version() -> None:
        import shutil
        import subprocess

        bash = shutil.which("bash")
        if bash is None:
            found = None
        else:
            out = subprocess.run(
                [bash, "--norc", "-c", 'echo "${BASH_VERSION}"'],
                stdout=subprocess.PIPE,
            )
            found = re.search(r"^(\d+)\.(\d+)\.\d+", out.stdout.decode())

        if found is None:
            echo(
                _("Couldn't detect Bash version, shell completion is not supported."),
                err=True,
            )
            return

        major, minor = int(found.group(1)), int(found.group(2))
        if (major, minor) < (4, 4):
            echo(
                _(
                    "Shell completion is not supported for Bash"
                    " versions older than 4.4."
                ),
                err=True,
            )

    def source(self) -> str:
        self._check_version()
        return super().source()

    def get_completion_args(self) -> tuple[list[str], str]:
        words = split_arg_string(os.environ["COMP_WORDS"])
        index = int(os.environ["COMP_CWORD"])
        args = words[1:index]
        incomplete = words[index] if index < len(words) else ""
        return args, incomplete

    def format_completion(self, item: CompletionItem) -> str:
        return f"{item.type},{item.value}"


class ZshComplete(ShellComplete):
    name = "zsh"
    source_template = _ZSH_SOURCE

    def get_completion_args(self) -> tuple[list[str], str]:
        words = split_arg_string(os.environ["COMP_WORDS"])
        index = int(os.environ["COMP_CWORD"])
        args = words[1:index]
        incomplete = words[index] if index < len(words) else ""
        return args, incomplete

    def format_completion(self, item: CompletionItem) -> str:
        help = item.help or "_"
        value = item.value
        # Items with a description go through ``_describe``, which splits
        # on the first unescaped colon, so colons must be escaped there.
        # ``compadd`` takes the value literally.
        if help != "_":
            value = value.replace(":", r"\:")
        return f"{item.type}\n{value}\n{help}"


class FishComplete(ShellComplete):
    name = "fish"
    source_template = _FISH_SOURCE

    def get_completion_args(self) -> tuple[list[str], str]:
        words = split_arg_string(os.environ["COMP_WORDS"])
        incomplete = os.environ["COMP_CWORD"]
        if incomplete:
            incomplete = split_arg_string(incomplete)[0]
        args = words[1:]
        # Fish includes the word being completed in the command line too.
        if incomplete and args and args[-1] == incomplete:
            args.pop()
        return args, incomplete

    def format_completion(self, item: CompletionItem) -> str:
        if item.help:
            # One completion per line: escape line breaks, and tabs are
            # the separator between value and help.
            help = item.help.replace("\n", "\\n").replace("\t", " ")
            return f"{item.type},{item.value}\t{help}"
        return f"{item.type},{item.value}"


class PowerShellComplete(ShellComplete):
    name = "powershell"
    source_template = _POWERSHELL_SOURCE

    def get_completion_args(self) -> tuple[list[str], str]:
        words = split_arg_string(os.environ["COMP_WORDS"])
        index = int(os.environ["COMP_CWORD"])
        args = words[1:index]
        incomplete = words[index] if index < len(words) else ""
        return args, incomplete

    def format_completion(self, item: CompletionItem) -> str:
        # Each item is three lines: kind, value, help ("_" for none). The
        # help must therefore stay on one line.
        help = item.help or "_"
        help = help.replace("\r", " ").replace("\n", " ")
        return f"{item.type}\n{item.value}\n{help}"


ShellCompleteType = t.TypeVar("ShellCompleteType", bound="type[ShellComplete]")

_available_shells: dict[str, type[ShellComplete]] = {
    "bash": BashComplete,
    "fish": FishComplete,
    "powershell": PowerShellComplete,
    "zsh": ZshComplete,
}


def add_completion_class(
    cls: ShellCompleteType, name: str | None = None
) -> ShellCompleteType:
    """Register a :class:`ShellComplete` subclass for a shell. Usable as a
    decorator.
    """
    _available_shells[name or cls.name] = cls
    return cls


def get_completion_class(shell: str) -> type[ShellComplete] | None:
    return _available_shells.get(shell)


def split_arg_string(string: str) -> list[str]:
    """Split a command line like a POSIX shell would. An unterminated
    quote or escape does not fail: the partial last word is kept.
    """
    import shlex

    lex = shlex.shlex(string, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    try:
        for token in lex:
            out.append(token)
    except ValueError:
        out.append(lex.token)
    return out


def _is_incomplete_argument(ctx: Context, param: Parameter) -> bool:
    """Whether ``param`` is an argument that can still take values."""
    if not isinstance(param, Argument):
        return False
    assert param.name is not None
    value = ctx.params.get(param.name)
    return (
        param.nargs == -1
        or ctx.get_parameter_source(param.name) is not ParameterSource.COMMANDLINE
        or (
            param.nargs > 1
            and isinstance(value, (tuple, list))
            and len(value) < param.nargs
        )
    )


def _start_of_option(ctx: Context, value: str) -> bool:
    return bool(value) and value[0] in ctx._opt_prefixes


def _is_incomplete_option(ctx: Context, args: list[str], param: Parameter) -> bool:
    """Whether the last words are ``param``'s name followed by fewer
    values than it takes.
    """
    if not isinstance(param, Option) or param.is_flag or param.count:
        return False

    last_option = None
    for seen, arg in enumerate(reversed(args), start=1):
        if seen > param.nargs:
            break
        if _start_of_option(ctx, arg):
            last_option = arg
    return last_option is not None and last_option in param.opts


def _resolve_context(
    cli: Command,
    ctx_args: cabc.MutableMapping[str, t.Any],
    prog_name: str,
    args: list[str],
) -> Context:
    """Parse the complete words to find the context of the innermost
    command, without running anything.
    """
    ctx_args["resilient_parsing"] = True
    with cli.make_context(prog_name, args.copy(), **ctx_args) as ctx:
        remaining = ctx._protected_args + ctx.args

        while remaining:
            command = ctx.command
            if not isinstance(command, Group):
                break

            if not command.chain:
                name, cmd, remaining = command.resolve_command(ctx, remaining)
                if cmd is None:
                    return ctx
                with cmd.make_context(
                    name, remaining, parent=ctx, resilient_parsing=True
                ) as sub_ctx:
                    ctx = sub_ctx
                    remaining = ctx._protected_args + ctx.args
                continue

            # Chained groups: walk through each subcommand in turn.
            last = ctx
            while remaining:
                name, cmd, remaining = command.resolve_command(ctx, remaining)
                if cmd is None:
                    return ctx
                with cmd.make_context(
                    name,
                    remaining,
                    parent=ctx,
                    allow_extra_args=True,
                    allow_interspersed_args=False,
                    resilient_parsing=True,
                ) as sub_ctx:
                    last = sub_ctx
                    remaining = sub_ctx.args
            ctx = last
            remaining = [*last._protected_args, *last.args]

    return ctx


def _resolve_incomplete(
    ctx: Context, args: list[str], incomplete: str
) -> tuple[Command | Parameter, str]:
    """Decide what provides completions: the command (option or
    subcommand names), an option's value, or an argument's value.
    """
    # Shells differ in how they report ``--opt=val``; normalize to the
    # option name in ``args`` and the value as the incomplete part.
    if incomplete == "=":
        incomplete = ""
    elif "=" in incomplete and _start_of_option(ctx, incomplete):
        name, _, incomplete = incomplete.partition("=")
        args.append(name)

    # After ``--`` everything is a value, even if it looks like an option.
    if "--" not in args and _start_of_option(ctx, incomplete):
        return ctx.command, incomplete

    params = ctx.command.get_params(ctx)

    for param in params:
        if _is_incomplete_option(ctx, args, param):
            return param, incomplete

    for param in params:
        if _is_incomplete_argument(ctx, param):
            return param, incomplete

    return ctx.command, incomplete
