"""Write Codex thread names through a short-lived app-server process."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..codex_executable import resolve_codex_executable
from ..windows_batch import UnsafeBatchArgument, batch_command


class CodexWriterError(RuntimeError):
    """Base class for failures while reading or writing a Codex thread name."""


class CodexTimeoutError(CodexWriterError):
    """The app-server did not answer before the operation deadline."""


class CodexEOFError(CodexWriterError):
    """The app-server closed stdout before returning the expected response."""


class CodexProcessError(CodexWriterError):
    """The app-server could not start or exited unsuccessfully."""


class CodexRPCError(CodexWriterError):
    """The app-server returned a JSON-RPC error response."""

    def __init__(self, method: str, error: object):
        self.method = method
        self.error = error
        super().__init__(f"Codex app-server rejected {method}: {error!r}")


class CodexProtocolError(CodexWriterError):
    """The app-server returned malformed or unexpected protocol data."""


class CodexConflictError(CodexWriterError):
    """The native thread changed after the caller captured its expected state."""

    def __init__(
        self,
        actual_title: str | None,
        *,
        field: str = "title",
        expected: object = None,
        actual: object = None,
    ) -> None:
        self.actual_title = actual_title
        self.field = field
        self.expected = expected
        self.actual = actual_title if actual is None and field == "title" else actual
        super().__init__(
            f"Codex thread changed before rename ({field}: expected {expected!r}, "
            f"found {self.actual!r}); refresh the thread before retrying"
        )


class CodexVerificationError(CodexWriterError):
    """The app-server accepted a rename but read-back did not match it."""

    def __init__(self, expected_title: str, actual_title: str | None) -> None:
        self.expected_title = expected_title
        self.actual_title = actual_title
        super().__init__(
            f"Codex thread rename could not be verified: expected {expected_title!r}, "
            f"found {actual_title!r}; inspect the thread before retrying"
        )


# Longer aliases make the error taxonomy discoverable without breaking the terse
# names used by the coordinator contract.
CodexWriterTimeoutError = CodexTimeoutError
CodexWriterEOFError = CodexEOFError
CodexWriterProcessError = CodexProcessError
CodexWriterRPCError = CodexRPCError
CodexWriterProtocolError = CodexProtocolError
CodexWriterConflictError = CodexConflictError
CodexWriterVerificationError = CodexVerificationError


_EOF = object()
_ClientFactory = Callable[[str | list[str], dict[str, str], float], Any]


def _resolved_executable(executable: str | os.PathLike[str]) -> str:
    raw = os.fspath(executable)
    found = resolve_codex_executable(raw)
    if found is None:
        raise CodexProcessError(
            f"Codex executable {raw!r} was not found; install Codex or pass its full path"
        )
    return found


def _app_server_command(
    executable: str | os.PathLike[str], environ: dict[str, str]
) -> str | list[str]:
    """Build a shell-free command, except for Windows batch launchers."""

    resolved = _resolved_executable(executable)
    if Path(resolved).suffix.lower() not in {".cmd", ".bat"}:
        return [resolved, "app-server", "--stdio"]

    comspec_raw = environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not comspec_raw:
        raise CodexProcessError(
            f"Cannot launch Codex batch file {resolved!r}: COMSPEC is unavailable"
        )
    comspec = _resolved_executable(comspec_raw)
    # Only the resolved executable and fixed app-server arguments enter cmd.exe.
    # Thread ids, titles, and all other dynamic JSON are written to stdin.
    try:
        return batch_command(comspec, resolved, "app-server", "--stdio")
    except UnsafeBatchArgument as exc:
        raise CodexProcessError(f"Cannot launch Codex batch file safely: {exc}") from exc


class _StdioJsonRpcClient:
    """Line-delimited JSON-RPC transport with continuously drained pipes."""

    def __init__(
        self, argv: str | list[str], env: dict[str, str], timeout: float
    ) -> None:
        self.timeout = timeout
        self._next_id = 1
        self._stdout: queue.Queue[object] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=200)
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise CodexProcessError(
                f"Codex app-server could not start ({argv!r}): {exc}"
            ) from exc

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(self._process.stdout,),
            name="rename-codex-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._process.stderr,),
            name="rename-codex-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _drain_stdout(self, stream: Any) -> None:
        try:
            for line in stream:
                self._stdout.put(line)
        finally:
            self._stdout.put(_EOF)

    def _drain_stderr(self, stream: Any) -> None:
        for line in stream:
            self._stderr.append(line.rstrip())

    def _send(self, payload: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.closed:
            raise CodexEOFError("Codex app-server stdin closed before the request was sent")
        try:
            stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexEOFError(
                "Codex app-server closed stdin before the request was sent"
            ) from exc

    def notify(self, method: str, params: object | None = None) -> None:
        payload: dict[str, object] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def request(self, method: str, params: object) -> object:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexTimeoutError(
                    f"Codex app-server timed out waiting for {method} after {self.timeout:g}s"
                )
            try:
                item = self._stdout.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexTimeoutError(
                    f"Codex app-server timed out waiting for {method} after {self.timeout:g}s"
                ) from exc
            if item is _EOF:
                returncode = self._process.poll()
                if returncode is None:
                    try:
                        returncode = self._process.wait(timeout=min(self.timeout, 0.1))
                    except subprocess.TimeoutExpired:
                        pass
                if returncode not in (None, 0):
                    detail = self._stderr_detail()
                    raise CodexProcessError(
                        f"Codex app-server exited with code {returncode} while waiting for "
                        f"{method}{detail}"
                    )
                raise CodexEOFError(
                    f"Codex app-server closed stdout while waiting for {method}"
                )
            if not isinstance(item, str) or not item.strip():
                continue
            try:
                message = json.loads(item)
            except json.JSONDecodeError as exc:
                raise CodexProtocolError(
                    f"Codex app-server returned invalid JSON while waiting for {method}"
                ) from exc
            if not isinstance(message, dict):
                raise CodexProtocolError(
                    f"Codex app-server returned a non-object while waiting for {method}"
                )
            # Notifications may arrive between a request and its response.
            if "method" in message and "id" not in message:
                continue
            if message.get("id") != request_id:
                raise CodexProtocolError(
                    f"Codex app-server returned response id {message.get('id')!r}; "
                    f"expected {request_id!r} for {method}"
                )
            if "error" in message:
                raise CodexRPCError(method, message["error"])
            if "result" not in message:
                raise CodexProtocolError(
                    f"Codex app-server response for {method} has no result"
                )
            return message["result"]

    def _stderr_detail(self) -> str:
        if not self._stderr:
            return ""
        return f"; stderr: {' | '.join(self._stderr)}"

    def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            returncode = self._process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                returncode = self._process.wait(timeout=min(self.timeout, 2.0))
            except subprocess.TimeoutExpired:
                self._process.kill()
                returncode = self._process.wait(timeout=2.0)
        self._stdout_thread.join(timeout=1.0)
        self._stderr_thread.join(timeout=1.0)
        if returncode:
            raise CodexProcessError(
                f"Codex app-server exited with code {returncode}{self._stderr_detail()}"
            )


class CodexWriter:
    """Compare, rename, and verify a Codex thread through app-server."""

    def __init__(
        self,
        codex_home: str | os.PathLike[str] | None = None,
        executable: str | os.PathLike[str] = "codex",
        timeout: float = 10.0,
        client_factory: _ClientFactory | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.codex_home = Path(codex_home).expanduser() if codex_home is not None else None
        self.executable = executable
        self.timeout = float(timeout)
        self._client_factory = client_factory or _StdioJsonRpcClient

    def _client(self) -> Any:
        env = os.environ.copy()
        if self.codex_home is not None:
            env["CODEX_HOME"] = str(self.codex_home)
        argv = _app_server_command(self.executable, env)
        return self._client_factory(argv, env, self.timeout)

    @staticmethod
    def _initialize(client: Any) -> None:
        result = client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "rename",
                    "title": "rename session title writer",
                    "version": "1",
                }
            },
        )
        if not isinstance(result, dict):
            raise CodexProtocolError("Codex app-server initialize result is not an object")
        client.notify("initialized")

    @staticmethod
    def _thread(client: Any, thread_id: str) -> dict[str, Any]:
        result = client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise CodexProtocolError("Codex app-server thread/read result has no thread object")
        thread = result["thread"]
        if thread.get("id") != thread_id:
            raise CodexProtocolError(
                f"Codex app-server thread/read returned thread id {thread.get('id')!r}; "
                f"expected {thread_id!r}"
            )
        if "name" in thread and thread["name"] is not None and not isinstance(thread["name"], str):
            raise CodexProtocolError("Codex app-server thread/read returned a non-string name")
        return thread

    @staticmethod
    def _seconds(value: object) -> object:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value / 1000) if value >= 100_000_000_000 else int(value)
        return value

    @staticmethod
    def _status(value: object) -> object:
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            return value["type"]
        return value

    def _run(self, operation: Callable[[Any], Any]) -> Any:
        client = self._client()
        operation_error: BaseException | None = None
        try:
            self._initialize(client)
            return operation(client)
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            try:
                client.close()
            except Exception:
                if operation_error is None:
                    raise

    def read_title(self, thread_id: str) -> str | None:
        """Read one title in a fresh app-server session."""

        return self._run(lambda client: self._thread(client, thread_id).get("name"))

    def set_title(
        self,
        thread_id: str,
        title: str,
        *,
        expected_title: str | None,
        expected_updated_at: int | float | None = None,
        expected_status: object | None = None,
    ) -> None:
        """Compare native state, set its title, and strictly verify read-back."""

        def operation(client: Any) -> None:
            before = self._thread(client, thread_id)
            actual_title = before.get("name")
            if actual_title != expected_title:
                raise CodexConflictError(
                    actual_title,
                    field="title",
                    expected=expected_title,
                    actual=actual_title,
                )
            if expected_updated_at is not None:
                expected_updated = self._seconds(expected_updated_at)
                actual_updated = self._seconds(before.get("updatedAt"))
                if actual_updated != expected_updated:
                    raise CodexConflictError(
                        actual_title,
                        field="updatedAt",
                        expected=expected_updated,
                        actual=actual_updated,
                    )
            if expected_status is not None:
                expected_state = self._status(expected_status)
                actual_state = self._status(before.get("status"))
                if actual_state != expected_state:
                    raise CodexConflictError(
                        actual_title,
                        field="status",
                        expected=expected_state,
                        actual=actual_state,
                    )

            result = client.request(
                "thread/name/set", {"threadId": thread_id, "name": title}
            )
            if not isinstance(result, dict):
                raise CodexProtocolError(
                    "Codex app-server thread/name/set result is not an object"
                )
            after = self._thread(client, thread_id)
            actual_after = after.get("name")
            if actual_after != title:
                raise CodexVerificationError(title, actual_after)

        self._run(operation)


__all__ = [
    "CodexConflictError",
    "CodexEOFError",
    "CodexProcessError",
    "CodexProtocolError",
    "CodexRPCError",
    "CodexTimeoutError",
    "CodexVerificationError",
    "CodexWriter",
    "CodexWriterConflictError",
    "CodexWriterEOFError",
    "CodexWriterError",
    "CodexWriterProcessError",
    "CodexWriterProtocolError",
    "CodexWriterRPCError",
    "CodexWriterTimeoutError",
    "CodexWriterVerificationError",
]
