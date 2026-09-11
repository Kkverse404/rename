"""Safe construction of the unavoidable ``cmd.exe`` batch-launch boundary."""

from __future__ import annotations

import subprocess


class UnsafeBatchArgument(ValueError):
    """A batch launcher argument cannot be represented without cmd expansion."""


def batch_command(comspec: str, executable: str, *arguments: str) -> str:
    """Quote every token for cmd.exe and reject percent expansion.

    CreateProcess cannot execute ``.cmd``/``.bat`` files directly. Quoting every
    token keeps ``&``, parentheses, spaces, and Unicode inside arguments. Percent
    expansion occurs even inside cmd quotes, so paths/options containing ``%``
    fail explicitly instead of executing a different command.
    """

    values = (comspec, executable, *arguments)
    for value in values:
        if not isinstance(value, str) or any(char in value for char in ("\x00", "\r", "\n")):
            raise UnsafeBatchArgument(
                "Windows batch launcher arguments must be single-line strings"
            )
        if "%" in value:
            raise UnsafeBatchArgument(
                f"Windows batch launcher path/argument contains unsupported '%': {value!r}"
            )

    quoted: list[str] = []
    for value in (executable, *arguments):
        encoded = subprocess.list2cmdline([value])
        if not (encoded.startswith('"') and encoded.endswith('"')):
            encoded = f'"{encoded}"'
        quoted.append(encoded)
    comspec_token = subprocess.list2cmdline([comspec])
    if not (comspec_token.startswith('"') and comspec_token.endswith('"')):
        comspec_token = f'"{comspec_token}"'
    # A string is intentional. Passing the nested /S /C quoting as a list
    # makes subprocess escape the inner quotes before cmd.exe receives them.
    return f'{comspec_token} /d /v:off /s /c "{" ".join(quoted)}"'


__all__ = ["UnsafeBatchArgument", "batch_command"]
