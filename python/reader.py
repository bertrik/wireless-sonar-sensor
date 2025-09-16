#!/usr/bin/env python3
import argparse
import asyncio
import queue
import threading
from dataclasses import dataclass
from enum import IntFlag

from bleak import BleakClient, BleakError

NOTIFY_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
READ_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"


class BleSerialPort:
    def __init__(self, address, reconnect_delay=3):
        self.address = address
        self.loop = asyncio.new_event_loop()
        self.client = BleakClient(address, disconnected_callback=self._on_disconnect)
        self.rx_queue = queue.Queue()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._reconnect_delay = reconnect_delay
        self._closing = False

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def open(self):
        self._thread.start()
        self._call(self._connect())  # wait until connected

    async def _connect(self):
        print(f"Connecting...")
        await self.client.connect()
        if not self.client.is_connected:
            raise BleakError("Connection failed")
        await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        print(f"Connected to {self.address}")

    def _on_disconnect(self, client):
        if not self._closing:
            print("Disconnected, retrying...")
            asyncio.run_coroutine_threadsafe(self._reconnect(), self.loop)

    async def _reconnect(self):
        while not self._closing:
            try:
                await self._connect()
                return
            except Exception:
                await asyncio.sleep(self._reconnect_delay)

    def _on_notify(self, _sender, data: bytearray):
        for b in data:
            self.rx_queue.put(bytes([b]))

    def write(self, data: bytes, timeout=None):
        return self._call(self.client.write_gatt_char(WRITE_UUID, data), timeout)

    def read(self, timeout=None) -> bytes | None:
        try:
            return self.rx_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._closing = True
        self._call(self._disconnect())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join()
        self.loop.close()

    async def _disconnect(self):
        if self.client.is_connected:
            await self.client.stop_notify(NOTIFY_UUID)
            await self.client.disconnect()
        print("Disconnected cleanly")

    def _call(self, coro, timeout=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()


class Protocol:
    NOISE_OFF = 0
    NOISE_LOW = 1
    NOISE_MEDIUM = 2
    NOISE_HIGH = 3

    RANGE_AUTO = 0
    RANGE_10FT = 1
    RANGE_20FT = 2
    RANGE_30FT = 3
    RANGE_60FT = 4
    RANGE_90FT = 5
    RANGE_120FT = 6
    RANGE_150FT = 7
    RANGE_200FT = 8

    def __init__(self):
        self.index = 0
        self.buffer = bytearray(140)

    def _checksum(self, data: bytes):
        sum = 0
        for b in data:
            sum += b
        return sum & 0xFF

    def build_configcmd(self, noise_filter, sensitivity, range) -> bytes:
        command = bytearray(10)
        command[0] = 0x53  # 'S'
        command[1] = 0x46  # 'F'
        command[2] = 1
        command[3] = noise_filter  # 0..3
        command[4] = sensitivity  # percent
        command[5] = 0
        command[6] = range  # 0=auto, 1=10ft, 2=20ft, 3=30ft, 4=60ft, 5=90ft, 6=120ft, 7=150ft, 8=200ft
        command[7] = 0
        command[8] = self._checksum(command[0:8])
        command[9] = 0x55  # 'U'
        return command

    def process(self, b) -> bytes | None:
        # store byte if it fits
        if self.index < len(self.buffer):
            self.buffer[self.index] = b
            # print(f" {b:02X}", end='')

        # check specific markers
        match self.index:
            case 0:
                if b != 0x53:  # 'S'
                    self.index = 0
                    return None
            case 1:
                if b != 0x46:  # 'F'
                    self.index = 0
                    return self.process(b)
            case 13:
                # calculate checksum
                check = self._checksum(self.buffer[0:13])
                if check != b:
                    self.index = 0
                    return self.process(b)
            case 14 | 136 | 138:
                if b != 0x55:
                    self.index = 0
                    return self.process(b)
            case 135 | 137 | 139:
                if b != 0xAA:
                    self.index = 0
                    return self.process(b)

        # next byte or done?
        self.index += 1
        if self.index == 140:
            self.index = 0
            return bytes(self.buffer)

        return None


@dataclass
class SensorData:
    class Status(IntFlag):
        CHARGING = 0x80
        CHARGE_DONE = 0x40
        OUT_OF_WATER = 0x08

    status: int
    bottom: int
    fishdepth: int
    fishsize: int
    battery: int
    temperature: int
    frequency: int
    depthrange: int
    rawdata: bytes

    @classmethod
    def from_bytes(cls, data: bytes):
        # Expect at least 13 bytes (since we access up to index 12)
        if len(data) < 13:
            raise ValueError(f"SensorData requires at least 13 bytes, got {len(data)}")
        return cls(
            status=data[2],
            bottom=(data[3] << 8) + data[4],
            fishdepth=(data[5] << 8) + data[6],
            fishsize=data[7],
            battery=data[8],
            temperature=(data[9] << 8) + data[10],
            frequency=data[11],
            depthrange=data[12],
            rawdata=data[15:135]
        )

    def _ft_to_meter(self, depth):
        return depth * 0.3048

    # status
    def get_status(self) -> set:
        return set(self.Status(self.status))

    # depth in meters
    def get_depth(self) -> float:
        return self._ft_to_meter(self.bottom / 10.0)

    # battery in percentage
    def get_battery(self) -> float:
        return self.battery * 100.0 / 6

    # temperature in degrees Celcius
    def get_temperature(self) -> float:
        return (self.temperature / 10.0 - 32) * 5 / 9

    # depth range in meters
    def get_depth_range(self) -> float:
        return self._ft_to_meter(self.depthrange)


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-d", "--device",
                        help="The FishHelper bluetooth device address", default="D3:01:01:02:2F:C6")
    args = parser.parse_args()

    print(f"Opening BleSerialPort '{args.device}'")
    with BleSerialPort(args.device) as port:
        protocol = Protocol()
        while True:
            data = port.read()
            for b in data:
                frame = protocol.process(b)
                if frame:
                    print(f"Frame: {frame.hex()}")
                    sd = SensorData.from_bytes(frame)
                    print(f"{sd}")
                    print(f"status={sd.get_status()},temp={sd.get_temperature():.1f}degC,batt={sd.get_battery():.1f}%,"
                          f"depth={sd.get_depth()}m,range={sd.get_depth_range()}m")


if __name__ == "__main__":
    main()
