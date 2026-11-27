"""Wire codec for the Limitless Pendant's protobuf-over-BLE protocol.

Pure functions, no I/O. The protocol was worked out by the Omi project
(https://github.com/BasedHardware/omi, MIT); this is an independent Python
implementation of the same wire format.

Shape of the protocol:

* Every packet in either direction is a "BLE wrapper" protobuf:
  1=message index, 2=fragment sequence, 3=fragment count, 4=payload.
  A payload can span several notifications and must be reassembled.
* Commands (written to TX) carry one message field plus a request trailer
  (field 30): 6=set time, 7=acknowledge pages, 8=set mode, 21=get status,
  15=unpair, 26=set LED brightness. No command to pause recording is known,
  and live-stream mode does not pause it: the pendant keeps writing to flash
  while it streams (measured: 15 pages in 20 s, the same as normal).
* In download mode each reassembled payload holds StorageBuffers (field 2):
  2=session, 4=sequence, 5=flash page index, 6=flash page data.
* A flash page starts with the pendant-clock timestamp (field 1, ms) and the
  time since boot (field 2, ms), followed by 0x1a wrappers whose 0x12 subfields
  contain Opus packets. Until the clock is set, field 1 equals field 2.
* Fields holding zero are omitted, so the first page after a reset has no index.
"""

from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "632de001-604c-446b-a80f-7963e950f3fb"
TX_CHAR_UUID = "632de002-604c-446b-a80f-7963e950f3fb"  # host -> pendant (write)
RX_CHAR_UUID = "632de003-604c-446b-a80f-7963e950f3fb"  # pendant -> host (notify)
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"  # standard battery level, 0-100

# A flash page holds about this much audio (measured: 1.4 to 1.5 s).
PAGE_SECONDS = 1.45

# Opus table-of-contents bytes the pendant's encoder is known to emit.
VALID_OPUS_TOC = frozenset({0xB8, 0x78, 0xF8, 0xB0, 0x70, 0xF0})

# Timestamps before 2020 mean the pendant clock was never set.
MIN_VALID_TIMESTAMP_MS = 1_577_836_800_000

_MAX_VARINT_BYTES = 10


# --- varints and field encoders ----------------------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Return (value, next position). Reads at most 10 bytes."""
    result = 0
    shift = 0
    read = 0
    while pos < len(data) and read < _MAX_VARINT_BYTES:
        byte = data[pos]
        pos += 1
        read += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return result, pos


def _int_field(field: int, value: int) -> bytes:
    return encode_varint(field << 3) + encode_varint(value)


def _bytes_field(field: int, value: bytes) -> bytes:
    return encode_varint((field << 3) | 2) + encode_varint(len(value)) + value


# --- commands -----------------------------------------------------------------


class CommandEncoder:
    """Builds command packets, owning the per-connection counters.

    Create a new encoder for every BLE connection: the pendant treats a
    reconnect as a new protocol session.
    """

    def __init__(self) -> None:
        self._message_index = 0
        self._request_id = 0

    def _command(self, field: int, body: bytes) -> bytes:
        self._request_id += 1
        trailer = _int_field(1, self._request_id) + _int_field(2, 0)
        payload = _bytes_field(field, body) + _bytes_field(30, trailer)
        packet = (
            _int_field(1, self._message_index)
            + _int_field(2, 0)
            + _int_field(3, 1)
            + _bytes_field(4, payload)
        )
        self._message_index += 1
        return packet

    def set_time(self, timestamp_ms: int) -> bytes:
        """Set the pendant clock (ms since the Unix epoch)."""
        return self._command(6, _int_field(1, timestamp_ms))

    def set_mode(self, *, download: bool, realtime: bool) -> bytes:
        """Switch device mode.

        download=True, realtime=False  -> push stored flash pages to the host
        download=False, realtime=True  -> live-stream audio
        download=False, realtime=False -> record to flash (the resting state)
        """
        return self._command(8, _int_field(1, int(download)) + _int_field(2, int(realtime)))

    def acknowledge(self, up_to_index: int) -> bytes:
        """Tell the pendant pages up to this index are safe. IT DELETES THEM."""
        return self._command(7, _int_field(1, up_to_index))

    def get_status(self) -> bytes:
        return self._command(21, b"")

    def unpair(self, reset: bool = False) -> bytes:
        """Make the pendant forget this host so another can pair with it.

        With reset=True the pendant also wipes itself. Nothing here uses that.
        """
        return self._command(15, _int_field(1, int(not reset)))

    def set_led_brightness(self, percent: int) -> bytes:
        """Set how bright the pendant's light is, 0 (off) to 100."""
        return self._command(26, _int_field(1, max(0, min(100, int(percent)))))


# --- incoming packets ---------------------------------------------------------


@dataclass(frozen=True)
class BlePacket:
    index: int
    seq: int
    num_frags: int
    payload: bytes


def parse_ble_packet(data: bytes) -> BlePacket | None:
    pos = 0
    index = num_frags = None
    seq = 0
    payload = None
    while pos < len(data):
        tag = data[pos]
        field, wire = tag >> 3, tag & 0x07
        pos += 1
        if wire == 0:
            value, pos = decode_varint(data, pos)
            if field == 1:
                index = value
            elif field == 2:
                seq = value
            elif field == 3:
                num_frags = value
        elif wire == 2:
            length, pos = decode_varint(data, pos)
            if length > len(data) - pos:
                if field == 4:
                    return None
                break
            if field == 4:
                payload = bytes(data[pos : pos + length])
            pos += length
        else:
            break
    if index is None or num_frags is None or payload is None:
        return None
    return BlePacket(index, seq, num_frags, payload)


class Reassembler:
    """Joins fragmented payloads. Feed packets; get a payload when complete."""

    def __init__(self, max_pending: int = 64) -> None:
        self._pending: dict[int, dict[int, bytes]] = {}
        self._max_pending = max_pending

    def add(self, packet: BlePacket) -> bytes | None:
        fragments = self._pending.setdefault(packet.index, {})
        fragments[packet.seq] = packet.payload
        if len(fragments) < packet.num_frags:
            while len(self._pending) > self._max_pending:
                del self._pending[next(iter(self._pending))]  # drop the oldest
            return None
        del self._pending[packet.index]
        return b"".join(fragments.get(i, b"") for i in range(packet.num_frags))

    def clear(self) -> None:
        self._pending.clear()


# --- flash pages --------------------------------------------------------------


@dataclass(frozen=True)
class FlashPage:
    index: int
    session: int
    seq: int
    data: bytes  # raw flash page bytes, kept so audio can be re-extracted later

    @property
    def timestamp_ms(self) -> int:
        return parse_page_timestamp_ms(self.data)

    @property
    def uptime_ms(self) -> int:
        return parse_page_uptime_ms(self.data)

    def opus_frames(self) -> list[bytes]:
        return extract_opus_frames(self.data)


def parse_pendant_message(payload: bytes) -> list[FlashPage]:
    """Extract every StorageBuffer (field 2) from a reassembled payload."""
    pages: list[FlashPage] = []
    pos = 0
    while pos < len(payload):
        tag = payload[pos]
        field, wire = tag >> 3, tag & 0x07
        pos += 1
        if wire == 2:
            length, pos = decode_varint(payload, pos)
            if length > len(payload) - pos:
                break
            if field == 2:
                page = _parse_storage_buffer(payload[pos : pos + length])
                if page is not None:
                    pages.append(page)
            pos += length
        elif wire == 0:
            _, pos = decode_varint(payload, pos)
        else:
            pos += 1
    return pages


def _parse_storage_buffer(buf: bytes) -> FlashPage | None:
    pos = 0
    session = seq = index = 0  # zero-valued fields are omitted on the wire
    page_data = None
    while pos < len(buf):
        tag = buf[pos]
        field, wire = tag >> 3, tag & 0x07
        pos += 1
        if wire == 0:
            value, pos = decode_varint(buf, pos)
            if field == 2:
                session = value
            elif field == 4:
                seq = value
            elif field == 5:
                index = value
        elif wire == 2:
            length, pos = decode_varint(buf, pos)
            if length > len(buf) - pos:
                break
            if field == 6:
                page_data = bytes(buf[pos : pos + length])
            pos += length
        else:
            pos += 1
    if not page_data:
        return None
    return FlashPage(index=index, session=session, seq=seq, data=page_data)


def parse_page_timestamp_ms(page_data: bytes) -> int:
    if page_data and page_data[0] == 0x08:
        return decode_varint(page_data, 1)[0]
    return 0


def parse_page_uptime_ms(page_data: bytes) -> int:
    """Milliseconds since the pendant booted (field 2), or 0 if absent."""
    pos = 0
    if page_data and page_data[0] == 0x08:
        _, pos = decode_varint(page_data, 1)
    if pos < len(page_data) and page_data[pos] == 0x10:
        return decode_varint(page_data, pos + 1)[0]
    return 0


def is_valid_opus_toc(byte: int) -> bool:
    return byte in VALID_OPUS_TOC


def extract_opus_frames(page_data: bytes) -> list[bytes]:
    """Walk the 0x1a audio wrappers of a flash page and collect Opus packets."""
    frames: list[bytes] = []
    n = len(page_data)
    pos = 0
    for leading in (0x08, 0x10):  # timestamp, then time since boot
        if pos < n and page_data[pos] == leading:
            _, pos = decode_varint(page_data, pos + 1)

    while pos < n - 2:
        if page_data[pos] != 0x1A:
            pos += 1
            continue
        wrapper_len, pos = decode_varint(page_data, pos + 1)
        if wrapper_len > n - pos:
            break
        wrapper_end = pos + wrapper_len
        while pos < wrapper_end - 1:
            marker = page_data[pos]
            if marker == 0x08:  # offset
                _, pos = decode_varint(page_data, pos + 1)
                continue
            if marker == 0x12:  # audio
                audio_len, pos = decode_varint(page_data, pos + 1)
                if audio_len > n - pos:
                    break
                _extract_opus_recursive(page_data, pos, pos + audio_len, frames)
                pos += audio_len
                continue
            wire = marker & 0x07
            pos += 1
            if wire == 0:
                _, pos = decode_varint(page_data, pos)
            elif wire == 2:
                length, nxt = decode_varint(page_data, pos)
                if length > n:
                    break
                pos = nxt + length
        pos = wrapper_end
    return frames


_MAX_DEPTH = 16  # real pages nest a few levels; anything deeper is not audio


def _extract_opus_recursive(data: bytes, start: int, end: int, frames: list[bytes], depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    pos = start
    while pos < end - 1 and pos < len(data):
        wire = data[pos] & 0x07
        pos += 1
        if wire == 2:
            length, pos = decode_varint(data, pos)
            if length > len(data):
                return
            if length > 0 and pos + length <= end:
                field = data[pos : pos + length]
                if 10 <= length <= 200 and is_valid_opus_toc(field[0]):
                    frames.append(bytes(field))
                elif length > 10:
                    _extract_opus_recursive(data, pos, pos + length, frames, depth + 1)
            pos += length
        elif wire == 0:
            _, pos = decode_varint(data, pos)
        else:
            break


# --- device status ------------------------------------------------------------


@dataclass(frozen=True)
class StorageState:
    oldest_flash_page: int
    newest_flash_page: int
    current_storage_session: int
    free_capture_pages: int
    total_capture_pages: int

    @property
    def page_count(self) -> int:
        return max(0, self.newest_flash_page - self.oldest_flash_page + 1)


def parse_device_status(data: bytes) -> StorageState | None:
    """Find a DeviceStatus (0x22 -> 0x2a -> 0x2a storage struct) in a notification."""
    n = len(data)
    if n < 20:
        return None
    pos = 0
    while pos < n - 5:
        if data[pos] != 0x22:
            pos += 1
            continue
        payload_len, pos = decode_varint(data, pos + 1)
        if payload_len < 10 or payload_len > n - pos:
            return None
        payload_end = pos + payload_len
        inner = pos
        while inner < payload_end - 5:
            if data[inner] != 0x2A:
                inner += 1
                continue
            status_len, inner = decode_varint(data, inner + 1)
            if status_len < 5 or status_len > 500 or status_len > n - inner:
                return None
            return _parse_storage_state(data, inner, inner + status_len)
        return None
    return None


_STORAGE_FIELDS = {
    0x08: "oldest_flash_page",
    0x10: "newest_flash_page",
    0x18: "current_storage_session",
    0x20: "free_capture_pages",
    0x28: "total_capture_pages",
}


def _parse_storage_state(data: bytes, start: int, end: int) -> StorageState | None:
    n = len(data)
    pos = start
    while pos < end - 1 and pos < n:
        if data[pos] != 0x2A:
            pos += 1
            continue
        storage_len, pos = decode_varint(data, pos + 1)
        if storage_len > 200 or storage_len > n - pos:
            return None
        storage_end = pos + storage_len
        values: dict[str, int] = {}
        while pos < storage_end - 1 and pos < n:
            marker = data[pos]
            pos += 1
            if pos >= n:
                break
            if marker in _STORAGE_FIELDS:
                values[_STORAGE_FIELDS[marker]], pos = decode_varint(data, pos)
        if not values:
            return None
        return StorageState(**{name: values.get(name, 0) for name in _STORAGE_FIELDS.values()})
    return None
