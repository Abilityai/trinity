"""Read-only policy adapter port and bounded local JSON Lines transport."""
from __future__ import annotations

import math
import threading
import time
from typing import BinaryIO, Callable, Protocol

from .contracts import (
    MAX_MESSAGE_BYTES,
    AdapterDecision,
    AdapterRequest,
    ContractValidationError,
    parse_adapter_decision_json,
    serialize_adapter_request,
)


_READ_LIMIT = MAX_MESSAGE_BYTES + 2
_MAX_DEADLINE_SECONDS = 300.0
_MAX_LIVE_CHANNELS = 256


class PortExchangeError(RuntimeError):
    """Raised when a bounded local JSON Lines exchange fails closed."""


class PolicyAdapterPort(Protocol):
    """Policy boundary that observes current state before making one decision."""

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        """Return one typed decision from read-only current observations."""
        ...


class JsonLinesExchangePort(Protocol):
    """Exchange one bounded JSON line through a template-provisioned channel."""

    @property
    def channel_identity(self) -> tuple[object, object]:
        """Return the underlying reader/writer identity pair."""
        ...

    @property
    def cleanup_complete(self) -> threading.Event:
        """Signal when both channel resources have finished closing."""
        ...

    @property
    def release_complete(self) -> threading.Event:
        """Signal when both I/O and resource cleanup have finished."""
        ...

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        """Bounded-clean this fresh port without closing a live shared half."""
        ...

    def exchange(self, request_line: str) -> str:
        """Write one request line and read at most one capped response line."""
        ...


JsonLinesExchangeFactory = Callable[[], JsonLinesExchangePort]
ChannelAdmission = object
LiveChannel = tuple[object, object, threading.Event, ChannelAdmission]

_CHANNEL_STATE_LOCK = threading.Lock()
_CHANNEL_ADMISSIONS: set[ChannelAdmission] = set()
_LIVE_CHANNELS: list[LiveChannel] = []


class _ChannelClaimRejected(PortExchangeError):
    def __init__(self, message: str, preserve: tuple[object, ...] = ()) -> None:
        super().__init__(message)
        self.preserve = preserve


class BoundedJsonLinesExchange:
    """One deadline-bound exchange over fresh template-owned binary streams.

    Each instance accepts exactly one request, so queued output cannot answer a
    later observation. The streams are closed after every outcome; cleanup is
    asynchronous when a close stalls. No wake or response can select a program,
    endpoint, environment, credential, or file path.
    """

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        deadline_seconds: float,
    ) -> None:
        if not hasattr(reader, "readline") or not hasattr(reader, "close"):
            raise TypeError("reader must be a closeable binary stream")
        if (
            not hasattr(writer, "write")
            or not hasattr(writer, "flush")
            or not hasattr(writer, "close")
        ):
            raise TypeError("writer must be a closeable binary stream")
        if (
            isinstance(deadline_seconds, bool)
            or not isinstance(deadline_seconds, (int, float))
            or not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or deadline_seconds > _MAX_DEADLINE_SECONDS
        ):
            raise ValueError("deadline_seconds must be between 0 and 300")
        self._reader = reader
        self._writer = writer
        self._deadline_seconds = float(deadline_seconds)
        self._state_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._cleanup_events: tuple[threading.Event, ...] | None = None
        self._cleanup_complete = threading.Event()
        self._io_complete = threading.Event()
        self._release_complete = threading.Event()
        self._used = False

    @property
    def channel_identity(self) -> tuple[object, object]:
        return (self._reader, self._writer)

    @property
    def cleanup_complete(self) -> threading.Event:
        return self._cleanup_complete

    @property
    def release_complete(self) -> threading.Event:
        return self._release_complete

    def reject(self, *, preserve: tuple[object, ...] = ()) -> None:
        """Close an unclaimed channel without creating an exchange worker."""
        with self._state_lock:
            if not self._used:
                self._used = True
                self._io_complete.set()
        self._cleanup_until(time.monotonic(), preserve=preserve)
        self._update_release_complete()

    def exchange(self, request_line: str) -> str:
        deadline = time.monotonic() + self._deadline_seconds
        request_bytes = request_line.encode("utf-8")
        if len(request_bytes) > MAX_MESSAGE_BYTES or b"\n" in request_bytes:
            self.reject()
            raise PortExchangeError("request is not one bounded JSON line")
        with self._state_lock:
            if self._used:
                raise PortExchangeError("a fresh exchange is required for each request")
            self._used = True

        completed = threading.Event()
        response: list[bytes] = []
        failure: list[BaseException] = []

        def exchange_once() -> None:
            try:
                outbound = request_bytes + b"\n"
                written = self._writer.write(outbound)
                if written is not None and written != len(outbound):
                    raise PortExchangeError(
                        "JSON Lines request was only partially written"
                    )
                self._writer.flush()
                value = self._reader.readline(_READ_LIMIT)
                if not isinstance(value, bytes):
                    raise PortExchangeError("JSON Lines response must be bytes")
                response.append(value)
            except BaseException as error:
                failure.append(error)
            finally:
                completed.set()
                self._io_complete.set()
                self._update_release_complete()

        worker = threading.Thread(target=exchange_once, daemon=True)
        worker.start()
        if not completed.wait(max(0.0, deadline - time.monotonic())):
            self._cleanup_until(deadline)
            raise PortExchangeError("template-controlled JSON Lines port timed out")
        self._cleanup_until(deadline)
        if failure:
            error = failure[0]
            if isinstance(error, PortExchangeError):
                raise error
            raise PortExchangeError("template-controlled JSON Lines port failed") from error
        if not response:
            raise PortExchangeError("port returned no JSON line")
        return _decode_response(response[0])

    def _cleanup_until(
        self,
        deadline: float,
        *,
        preserve: tuple[object, ...] = (),
    ) -> None:
        with self._cleanup_lock:
            if self._cleanup_events is None:
                streams: list[object] = []
                seen: set[int] = set()
                for stream in (self._reader, self._writer):
                    if id(stream) not in seen and not any(
                        stream is preserved for preserved in preserve
                    ):
                        seen.add(id(stream))
                        streams.append(stream)
                cleanup_events = tuple(threading.Event() for _ in streams)
                self._cleanup_events = cleanup_events

                if not cleanup_events:
                    self._cleanup_complete.set()
                    self._update_release_complete()

                def close_stream(target: object, done: threading.Event) -> None:
                    try:
                        target.close()  # type: ignore[attr-defined]
                    except BaseException:
                        pass
                    finally:
                        done.set()
                        if all(event.is_set() for event in cleanup_events):
                            self._cleanup_complete.set()
                            self._update_release_complete()

                for stream, completed in zip(streams, cleanup_events, strict=True):
                    threading.Thread(
                        target=close_stream,
                        args=(stream, completed),
                        daemon=True,
                    ).start()
            cleanup_events = self._cleanup_events

        for completed in cleanup_events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            completed.wait(remaining)

    def _update_release_complete(self) -> None:
        if self._io_complete.is_set() and self._cleanup_complete.is_set():
            self._release_complete.set()


class JsonLinesPolicyAdapter:
    """Parse each decision from a fresh bounded template-owned channel."""

    def __init__(self, exchange_factory: JsonLinesExchangeFactory) -> None:
        if not callable(exchange_factory):
            raise TypeError("exchange_factory must be callable")
        self._exchange_factory = exchange_factory

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        request_line = serialize_adapter_request(request)
        admission = _claim_channel_admission()
        if admission is None:
            raise PortExchangeError("too many channels are still live")
        try:
            exchange = self._exchange_factory()
        except Exception as error:
            _release_channel_admission(admission)
            raise PortExchangeError("exchange factory failed") from error
        if not hasattr(exchange, "exchange"):
            _reject_exchange(exchange, admission)
            raise PortExchangeError("exchange_factory did not provide a JSON Lines port")
        try:
            identity = _claim_live_channel(exchange, admission)
        except _ChannelClaimRejected as error:
            _reject_exchange(exchange, admission, preserve=error.preserve)
            raise PortExchangeError(str(error)) from error
        try:
            response_line = exchange.exchange(request_line)
        finally:
            _release_live_channel(identity)
        try:
            return parse_adapter_decision_json(response_line)
        except ContractValidationError as error:
            raise PortExchangeError("adapter returned an invalid closed decision") from error


def _decode_response(response: bytes) -> str:
    if len(response) >= _READ_LIMIT:
        raise PortExchangeError("JSON Lines response exceeds 1 MiB")
    if response.endswith(b"\n"):
        response = response[:-1]
    if b"\n" in response or b"\r" in response:
        raise PortExchangeError("port returned more than one JSON line")
    if len(response) > MAX_MESSAGE_BYTES:
        raise PortExchangeError("JSON Lines response exceeds 1 MiB")
    if not response:
        raise PortExchangeError("port returned an empty JSON line")
    try:
        return response.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PortExchangeError("JSON Lines response is not UTF-8") from error


def _claim_live_channel(
    exchange: JsonLinesExchangePort,
    admission: ChannelAdmission,
) -> LiveChannel:
    identity = getattr(exchange, "channel_identity", None)
    if not isinstance(identity, tuple) or len(identity) != 2:
        raise _ChannelClaimRejected(
            "exchange did not expose a fresh channel identity"
        )
    release_complete = getattr(exchange, "release_complete", None)
    if not isinstance(release_complete, threading.Event):
        raise _ChannelClaimRejected("exchange did not expose bounded release state")
    with _CHANNEL_STATE_LOCK:
        _purge_released_channels_locked()
        preserved = tuple(
            candidate
            for position, candidate in enumerate(identity)
            if not any(candidate is prior for prior in identity[:position])
            and any(
                candidate is existing
                for reader, writer, _, _ in _LIVE_CHANNELS
                for existing in (reader, writer)
            )
        )
        if preserved:
            raise _ChannelClaimRejected(
                "a fresh channel is required while a channel is live",
                preserved,
            )
        channel = (identity[0], identity[1], release_complete, admission)
        _LIVE_CHANNELS.append(channel)
        return channel


def _claim_channel_admission() -> ChannelAdmission | None:
    with _CHANNEL_STATE_LOCK:
        _purge_released_channels_locked()
        if len(_CHANNEL_ADMISSIONS) >= _MAX_LIVE_CHANNELS:
            return None
        admission = object()
        _CHANNEL_ADMISSIONS.add(admission)
        return admission


def _reject_exchange(
    exchange: object,
    admission: ChannelAdmission,
    *,
    preserve: tuple[object, ...] = (),
) -> None:
    def reject_and_release() -> None:
        reject = getattr(exchange, "reject", None)
        if not callable(reject):
            return
        try:
            reject(preserve=preserve)
        except BaseException:
            return
        release_complete = getattr(exchange, "release_complete", None)
        if not isinstance(release_complete, threading.Event):
            return
        release_complete.wait()
        _release_channel_admission(admission)

    threading.Thread(
        target=reject_and_release,
        name="delivery-conductor-reject-cleanup",
        daemon=True,
    ).start()


def _release_live_channel(
    channel: LiveChannel,
) -> None:
    def release_when_closed() -> None:
        channel[2].wait()
        with _CHANNEL_STATE_LOCK:
            for index, current in enumerate(_LIVE_CHANNELS):
                if current is channel:
                    del _LIVE_CHANNELS[index]
                    break
            _CHANNEL_ADMISSIONS.discard(channel[3])

    if channel[2].is_set():
        release_when_closed()
    else:
        threading.Thread(target=release_when_closed, daemon=True).start()


def _release_channel_admission(admission: ChannelAdmission) -> None:
    with _CHANNEL_STATE_LOCK:
        _CHANNEL_ADMISSIONS.discard(admission)


def _purge_released_channels_locked() -> None:
    retained: list[LiveChannel] = []
    for channel in _LIVE_CHANNELS:
        if channel[2].is_set():
            _CHANNEL_ADMISSIONS.discard(channel[3])
        else:
            retained.append(channel)
    _LIVE_CHANNELS[:] = retained
