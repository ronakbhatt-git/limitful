"""Bluetooth client: find the pendant, read its status, download stored audio."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .protocol import (
    BATTERY_CHAR_UUID,
    PAGE_SECONDS,
    RX_CHAR_UUID,
    SERVICE_UUID,
    TX_CHAR_UUID,
    CommandEncoder,
    FlashPage,
    Reassembler,
    StorageState,
    parse_ble_packet,
    parse_device_status,
    parse_pendant_message,
)
from .store import Store

STATUS_TIMEOUT_S = 8
STALL_TIMEOUT_S = 30
CHECKPOINT_EVERY_PAGES = 25
PAIRING_TIMEOUT_S = 60

_NOT_PAIRED = (
    "the pendant refused the connection because this computer is not paired "
    "with it. Put the pendant in pairing mode (press and release the button, then "
    "immediately press and hold until the purple light goes out; blue means ready "
    "to pair; this erases audio stored on the pendant), accept the pairing "
    "request on this computer, and run the command again. To pair without "
    "erasing, plug the pendant in by USB and use Pair without erasing under "
    "Settings, Pendant in the Lifelog."
)
_DROPPED = (
    "the pendant kept dropping the connection. Check it is nearby, charged and "
    "not connected to a phone, then run the command again. If the pendant was "
    "unpaired or reset since this computer last used it, the computer is still "
    "holding its old pairing: remove Pendant under Bluetooth in System Settings "
    "(on Windows: Settings, Bluetooth & devices), turn Bluetooth off and on, then "
    "pair again."
)
PAIRING_RETRY_S = 2
_SETTLE_S = 1.0  # the pendant needs a moment after connect and after subscribing


class PendantError(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    reason: str  # "caught_up", "stalled" or "empty"
    pages_received: int
    pages_new: int
    acked_up_to: int | None
    pages_discarded: int = 0  # recorded while paused, so deliberately not kept


async def scan(timeout: float = 8.0) -> list[tuple[BLEDevice, int]]:
    """Return (device, rssi) for every pendant advertising nearby."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    pendants = []
    for device, adv in found.values():
        name = (adv.local_name or device.name or "").lower()
        uuids = [u.lower() for u in adv.service_uuids]
        if SERVICE_UUID in uuids or "limitless" in name or "pendant" in name:
            pendants.append((device, adv.rssi))
    return sorted(pendants, key=lambda item: -item[1])


async def find(timeout: float = 8.0) -> BLEDevice | None:
    """The first pendant seen advertising, returned as soon as it is seen."""
    def is_pendant(device: BLEDevice, adv) -> bool:
        name = (adv.local_name or device.name or "").lower()
        return SERVICE_UUID in [u.lower() for u in adv.service_uuids] or "limitless" in name or "pendant" in name

    return await BleakScanner.find_device_by_filter(is_pendant, timeout=timeout)


async def read_state(timeout: float = 8.0) -> dict:
    """Connect briefly and report battery and storage. Raises PendantError if out of reach."""
    device = await find(timeout)
    if device is None:
        raise PendantError(
            "the pendant is not in range, is switched off, or is connected to another device")
    async with Pendant(device, pairing_timeout=10) as pendant:
        state = await pendant.status()
        battery = await pendant.battery()
    return {
        "battery": battery,
        "pages": state.page_count,
        "free_pages": state.free_capture_pages,
        "total_pages": state.total_capture_pages,
        "minutes_waiting": round(state.page_count * PAGE_SECONDS / 60, 1),
        "hours_free": round(state.free_capture_pages * PAGE_SECONDS / 3600, 1),
    }


async def set_brightness(percent: int, timeout: float = 8.0) -> None:
    """Connect briefly and set the brightness of the pendant's light."""
    device = await find(timeout)
    if device is None:
        raise PendantError(
            "the pendant is not in range, is switched off, or is connected to another device")
    async with Pendant(device, pairing_timeout=10) as pendant:
        await pendant.set_led_brightness(percent)


async def unpair(timeout: float = 8.0) -> None:
    """Tell the pendant to forget this computer, keeping its recordings."""
    device = await find(timeout)
    if device is None:
        raise PendantError(
            "the pendant is not in range, is switched off, or is connected to another device")
    async with Pendant(device, pairing_timeout=10) as pendant:
        await pendant.unpair()


async def pair(
    timeout: float = 180.0, log: Callable[[str], None] = print
) -> StorageState | None:
    """Keep trying to reach the pendant while the user puts it in pairing mode.

    Returns the storage status once an encrypted command succeeds, or None if
    the time runs out. Logs what the pendant advertises whenever it changes, so
    a change of mode is visible even when the connection is still refused.
    """
    deadline = time.monotonic() + timeout
    last_seen: str | None = None
    while time.monotonic() < deadline:
        found = await BleakScanner.discover(timeout=3.0, return_adv=True)
        target = None
        for device, adv in found.values():
            name = (adv.local_name or device.name or "").lower()
            if SERVICE_UUID in [u.lower() for u in adv.service_uuids] or "pendant" in name:
                target = (device, adv)
                break
        if target is None:
            seen = "not advertising"
        else:
            data = ", ".join(f"{k:#06x}={v.hex()}" for k, v in target[1].manufacturer_data.items())
            seen = f"advertising ({data or 'no manufacturer data'})"
        if seen != last_seen:
            log(f"pendant: {seen}")
            last_seen = seen
        if target is None:
            continue
        try:
            async with Pendant(target[0], pairing_timeout=6) as pendant:
                return await pendant.status()
        except PendantError:
            continue  # still refused or dropped; keep waiting
        except (BleakError, asyncio.TimeoutError) as err:
            log(f"connection attempt failed: {err}")
    return None


class Pendant:
    """One connection to a pendant. Use as an async context manager."""

    def __init__(self, device: BLEDevice | str, *, pairing_timeout: float | None = None) -> None:
        # macOS pairs by itself when the first encrypted write is refused. Windows
        # does not, so there Bleak is asked to pair as part of connecting.
        self._client = BleakClient(device, pair=True) if sys.platform == "win32" else BleakClient(device)
        self._pairing_timeout = PAIRING_TIMEOUT_S if pairing_timeout is None else pairing_timeout
        self._encoder = CommandEncoder()
        self._reassembler = Reassembler()
        self._status_waiter: asyncio.Future[StorageState] | None = None
        self._pages: asyncio.Queue[FlashPage] | None = None

    async def __aenter__(self) -> "Pendant":
        """Connect, subscribe and set the clock.

        Setting the clock is the first encrypted write, so it is what triggers
        pairing. The pendant rejects it until the link is encrypted, and may
        drop the link while the operating system pairs (macOS shows a dialog),
        so keep reconnecting and retrying until the write goes through.
        """
        deadline = time.monotonic() + self._pairing_timeout
        try:
            while True:
                try:
                    if not self._client.is_connected:
                        self._encoder = CommandEncoder()  # a reconnect is a new session
                        self._reassembler.clear()
                        await self._client.connect()
                        await asyncio.sleep(_SETTLE_S)
                        await self._client.start_notify(RX_CHAR_UUID, self._on_notify)
                        await asyncio.sleep(_SETTLE_S)
                    await self._write(self._encoder.set_time(int(time.time() * 1000)))
                    return self
                except (BleakError, asyncio.TimeoutError) as err:
                    text = str(err).lower()
                    refused = "encryption" in text or "authentication" in text
                    if not refused and self._client.is_connected:
                        raise
                    if time.monotonic() >= deadline:
                        raise PendantError(_NOT_PAIRED if refused else _DROPPED) from None
                    await asyncio.sleep(PAIRING_RETRY_S)
        except BaseException:
            await self._disconnect_quietly()
            raise

    async def __aexit__(self, *exc) -> None:
        await self._disconnect_quietly()

    async def _disconnect_quietly(self) -> None:
        try:
            await self._client.disconnect()
        except (BleakError, asyncio.TimeoutError):
            pass

    async def _write(self, packet: bytes) -> None:
        await self._client.write_gatt_char(TX_CHAR_UUID, packet, response=True)

    def _on_notify(self, _sender, raw: bytearray) -> None:
        data = bytes(raw)
        self._offer_status(data)
        packet = parse_ble_packet(data)
        if packet is None:
            return
        payload = self._reassembler.add(packet)
        if payload is None:
            return
        self._offer_status(payload)
        if self._pages is not None:
            for page in parse_pendant_message(payload):
                self._pages.put_nowait(page)

    def _offer_status(self, data: bytes) -> None:
        waiter = self._status_waiter
        if waiter is None or waiter.done():
            return
        state = parse_device_status(data)
        if state is not None:
            waiter.set_result(state)

    async def battery(self) -> int | None:
        """Battery level in percent, or None if the pendant does not say."""
        try:
            data = await self._client.read_gatt_char(BATTERY_CHAR_UUID)
        except (BleakError, asyncio.TimeoutError):
            return None
        return int(data[0]) if data and data[0] <= 100 else None

    async def unpair(self) -> None:
        try:
            await self._write(self._encoder.unpair())
        except BleakError as err:
            # The pendant may drop the link as it obeys, before confirming the write.
            if self._client.is_connected:
                raise PendantError(f"the pendant did not accept the unpair command: {err}") from None

    async def set_led_brightness(self, percent: int) -> None:
        await self._write(self._encoder.set_led_brightness(percent))

    async def status(self) -> StorageState:
        self._status_waiter = asyncio.get_running_loop().create_future()
        try:
            await self._write(self._encoder.get_status())
            return await asyncio.wait_for(self._status_waiter, STATUS_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise PendantError("the pendant did not report its storage status") from None
        finally:
            self._status_waiter = None

    async def sync(
        self,
        store: Store,
        *,
        ack: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        """Download stored flash pages into the store.

        With ack=False nothing is removed from the pendant. With ack=True the
        pendant is told to free pages, but only after they are committed to
        disk and only up to the last page with no gap before it.
        """
        state = await self.status()
        if state.page_count == 0:
            return SyncResult("empty", 0, 0, None)

        end_page = state.newest_flash_page
        total = state.page_count
        received = new = 0
        max_seen = -1
        contiguous_to: int | None = None  # highest index with no gap before it
        gap = False
        acked_to: int | None = None
        uncommitted = 0
        reason = "stalled"
        discarded_before = store.discarded_pages

        async def checkpoint() -> None:
            nonlocal acked_to, uncommitted
            store.commit()
            uncommitted = 0
            if ack and contiguous_to is not None and contiguous_to != acked_to:
                await self._write(self._encoder.acknowledge(contiguous_to))
                acked_to = contiguous_to

        self._reassembler.clear()
        self._pages = asyncio.Queue()
        await self._write(self._encoder.set_mode(download=True, realtime=False))
        try:
            while True:
                try:
                    page = await asyncio.wait_for(self._pages.get(), STALL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    break
                received += 1
                if store.add_page(page):
                    new += 1
                uncommitted += 1
                if not gap:
                    if contiguous_to is None or page.index == contiguous_to + 1:
                        contiguous_to = page.index
                    elif page.index > contiguous_to + 1:
                        gap = True  # a page went missing: never ack past this point
                max_seen = max(max_seen, page.index)
                if on_progress:
                    on_progress(received, total)
                if uncommitted >= CHECKPOINT_EVERY_PAGES:
                    await checkpoint()
                if max_seen >= end_page:
                    reason = "caught_up"
                    break
        finally:
            self._pages = None
            try:
                await checkpoint()
            finally:
                # Always hand the pendant back to recording-to-flash.
                await self._write(self._encoder.set_mode(download=False, realtime=False))
        return SyncResult(reason, received, new, acked_to, store.discarded_pages - discarded_before)
