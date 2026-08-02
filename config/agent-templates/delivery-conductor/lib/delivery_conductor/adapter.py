"""Read-only policy adapter port and bounded local JSON Lines transport."""
from __future__ import annotations

import math
import threading
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

    def exchange(self, request_line: str) -> str:
        """Write one request line and read at most one capped response line."""
        ...


JsonLinesExchangeFactory = Callable[[], JsonLinesExchangePort]


class BoundedJsonLinesExchange:
    """One deadline-bound exchange over fresh template-owned binary streams.

    Each instance accepts exactly one request, so queued output cannot answer a
    later observation. The streams are closed on timeout to cancel a stalled
    write or read. No wake or response can select a program, endpoint,
    environment, credential, or file path.
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
        self._used = False

    @property
    def channel_identity(self) -> tuple[object, object]:
        return (self._reader, self._writer)

    def exchange(self, request_line: str) -> str:
        request_bytes = request_line.encode("utf-8")
        if len(request_bytes) > MAX_MESSAGE_BYTES or b"\n" in request_bytes:
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

        worker = threading.Thread(target=exchange_once, daemon=True)
        worker.start()
        if not completed.wait(self._deadline_seconds):
            self._cancel()
            completed.wait(0.1)
            raise PortExchangeError("template-controlled JSON Lines port timed out")
        if failure:
            error = failure[0]
            if isinstance(error, PortExchangeError):
                raise error
            raise PortExchangeError("template-controlled JSON Lines port failed") from error
        if not response:
            raise PortExchangeError("port returned no JSON line")
        return _decode_response(response[0])

    def _cancel(self) -> None:
        closed: set[int] = set()
        for stream in (self._reader, self._writer):
            if id(stream) in closed:
                continue
            closed.add(id(stream))
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class JsonLinesPolicyAdapter:
    """Parse each decision from a fresh bounded template-owned channel."""

    def __init__(self, exchange_factory: JsonLinesExchangeFactory) -> None:
        if not callable(exchange_factory):
            raise TypeError("exchange_factory must be callable")
        self._exchange_factory = exchange_factory
        self._used_channels: list[tuple[object, object]] = []
        self._channel_lock = threading.Lock()

    def observe_and_decide(self, request: AdapterRequest) -> AdapterDecision:
        request_line = serialize_adapter_request(request)
        exchange = self._exchange_factory()
        if not hasattr(exchange, "exchange"):
            raise PortExchangeError("exchange_factory did not provide a JSON Lines port")
        _claim_fresh_channel(exchange, self._used_channels, self._channel_lock)
        response_line = exchange.exchange(request_line)
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


def _claim_fresh_channel(
    exchange: JsonLinesExchangePort,
    used_channels: list[tuple[object, object]],
    lock: threading.Lock,
) -> None:
    identity = getattr(exchange, "channel_identity", None)
    if not isinstance(identity, tuple) or len(identity) != 2:
        raise PortExchangeError("exchange did not expose a fresh channel identity")
    with lock:
        if any(
            identity[0] is reader and identity[1] is writer
            for reader, writer in used_channels
        ):
            raise PortExchangeError("a fresh channel is required for each request")
        used_channels.append(identity)
