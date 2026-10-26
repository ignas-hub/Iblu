"""A recording stand-in for a googleapiclient service.

The Google client is used as a chain — `service.users().messages().modify(
userId="me", id=..., body=...).execute()` — and what a test wants to pin is
the final method's kwargs. `Recorder` accepts any chain, records every call
as ("users.messages.modify", kwargs), and answers `.execute()` from a
`responses` map keyed by that dotted path (a dict, or a callable taking the
kwargs). Nothing here reaches the network.

Shared by the four Workspace tool test modules; it lives under `tests/`
without a `test_` prefix so pytest never collects it.
"""

from __future__ import annotations

from typing import Any


class Recorder:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses: dict[str, Any] = dict(responses or {})
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str) -> "_Node":
        return _Node(self, (name,))

    def calls_to(self, path: str) -> list[dict]:
        """Every kwargs dict recorded for one dotted path, in order."""
        return [kw for p, kw in self.calls if p == path]

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


class _Node:
    def __init__(self, rec: Recorder, path: tuple[str, ...]) -> None:
        self._rec = rec
        self._path = path

    def __getattr__(self, name: str) -> "_Node":
        return _Node(self._rec, self._path + (name,))

    def __call__(self, **kwargs) -> "_Exec":
        key = ".".join(self._path)
        self._rec.calls.append((key, kwargs))
        reply: Any = self._rec.responses.get(key, {})
        if callable(reply):
            reply = reply(kwargs)
        return _Exec(reply, self._rec, self._path)


class _Exec(_Node):
    def __init__(self, result: Any, rec: Recorder, path: tuple[str, ...]) -> None:
        super().__init__(rec, path)
        self._result = result

    def execute(self) -> Any:
        return self._result
