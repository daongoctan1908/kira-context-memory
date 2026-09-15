"""Optional invocation-local observation hooks with no telemetry dependency."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator, Mapping, Optional, Protocol


class Observation(Protocol):
    def set_attribute(self, key: str, value: object) -> None: ...

    def set_outcome(self, outcome: str) -> None: ...

    def set_input(self, value: object) -> None: ...

    def set_output(self, value: object) -> None: ...

    def set_usage(self, usage: Mapping[str, object]) -> None: ...


class Observer(Protocol):
    def observe(
        self,
        name: str,
        *,
        kind: str = "internal",
        attributes: Optional[Mapping[str, object]] = None,
    ) -> Any: ...


class _NoOpObservation:
    def set_attribute(self, key: str, value: object) -> None:
        pass

    def set_outcome(self, outcome: str) -> None:
        pass

    def set_input(self, value: object) -> None:
        pass

    def set_output(self, value: object) -> None:
        pass

    def set_usage(self, usage: Mapping[str, object]) -> None:
        pass


class _SafeObservation:
    """Prevent a caller-provided observer from affecting memory behavior."""

    def __init__(self, observation: object) -> None:
        self._observation = observation

    def _call(self, method: str, *args: object) -> None:
        try:
            callback = getattr(self._observation, method, None)
            if callable(callback):
                callback(*args)
        except Exception:
            pass

    def set_attribute(self, key: str, value: object) -> None:
        self._call("set_attribute", key, value)

    def set_outcome(self, outcome: str) -> None:
        self._call("set_outcome", outcome)

    def set_input(self, value: object) -> None:
        self._call("set_input", value)

    def set_output(self, value: object) -> None:
        self._call("set_output", value)

    def set_usage(self, usage: Mapping[str, object]) -> None:
        self._call("set_usage", usage)


_NOOP = _NoOpObservation()
_current_observer: ContextVar[Optional[Observer]] = ContextVar("mem0_observer", default=None)
_current_observation: ContextVar[Observation] = ContextVar("mem0_observation", default=_NOOP)


@contextmanager
def bind_observer(observer: Optional[Observer]) -> Iterator[None]:
    """Bind one observer to this invocation and child ``asyncio.to_thread`` calls."""
    token = _current_observer.set(observer)
    try:
        yield
    finally:
        _current_observer.reset(token)


@contextmanager
def observe(
    name: str,
    *,
    kind: str = "internal",
    attributes: Optional[Mapping[str, object]] = None,
) -> Iterator[Observation]:
    """Open a fail-open observation and retain the business exception unchanged."""
    observer = _current_observer.get()
    if observer is None:
        yield _NOOP
        return

    manager = None
    try:
        manager = observer.observe(name, kind=kind, attributes=attributes)
        raw_observation = manager.__enter__()
        observation = _SafeObservation(raw_observation)
    except Exception:
        yield _NOOP
        return

    token = _current_observation.set(observation)
    error: Optional[BaseException] = None
    try:
        yield observation
    except BaseException as caught:
        error = caught
        observation.set_attribute("error.type", type(caught).__name__)
        observation.set_outcome("error")
        raise
    finally:
        _current_observation.reset(token)
        try:
            if error is None:
                manager.__exit__(None, None, None)
            else:
                manager.__exit__(type(error), error, error.__traceback__)
        except Exception:
            pass


def current_observation_attribute(key: str, value: object) -> None:
    _current_observation.get().set_attribute(key, value)


def current_observation_input(value: object) -> None:
    _current_observation.get().set_input(value)


def current_observation_output(value: object) -> None:
    _current_observation.get().set_output(value)


def current_observation_usage(usage: object) -> None:
    """Read common SDK usage objects without assuming one provider response type."""
    if usage is None:
        return
    values: Dict[str, object] = {}
    aliases = {
        "input": ("input_tokens", "prompt_tokens"),
        "output": ("output_tokens", "completion_tokens"),
        "total": ("total_tokens",),
    }
    for target, names in aliases.items():
        for name in names:
            try:
                value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
            except Exception:
                value = None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[target] = value
                break
    if values:
        _current_observation.get().set_usage(values)


def current_observation_response_usage(response: object) -> None:
    """Read a provider response's usage field without affecting provider semantics."""
    try:
        usage = getattr(response, "usage", None)
    except Exception:
        return
    current_observation_usage(usage)
