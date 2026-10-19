import asyncio
import json
import unittest

from supervisory.app.serial_interface import SerialInterface


class QueueReader:
    def __init__(self):
        self.lines = asyncio.Queue()

    async def readline(self):
        return await self.lines.get()


class ReactiveWriter:
    def __init__(self, reader, decision=None):
        self.reader = reader
        self.decision = decision
        self.payloads = []
        self.closed = False

    def write(self, payload):
        self.payloads.append(payload)
        command = json.loads(payload)
        if "rid" not in command or self.decision is None:
            return
        accepted = bool(self.decision(command))
        response = {
            "ack": accepted,
            "rid": command["rid"],
            "cmd": command["cmd"],
        }
        if not accepted:
            response["error"] = "TEST_REJECTION"
        self.reader.lines.put_nowait((json.dumps(response) + "\n").encode())

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class BlockingWriter(ReactiveWriter):
    def __init__(self, reader):
        super().__init__(reader)
        self.drain_started = asyncio.Event()
        self.release_drain = asyncio.Event()

    async def drain(self):
        self.drain_started.set()
        await self.release_drain.wait()


class TestSerialInterfaceAcknowledgements(unittest.TestCase):
    def test_only_heartbeat_is_allowed_without_application_ack(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(reader)
            interface.reader = reader
            interface.writer = writer
            interface.running = True

            self.assertFalse(
                await interface.send_command({"cmd": "PUMP_CONTROL"})
            )
            self.assertTrue(await interface.send_command({"cmd": "HEARTBEAT"}))

            self.assertEqual(
                [json.loads(payload) for payload in writer.payloads],
                [{"cmd": "HEARTBEAT"}],
            )

        asyncio.run(run_test())

    def test_request_id_wraps_inside_uint16_application_range(self):
        interface = SerialInterface()
        interface._next_request_id = 65535

        self.assertEqual(interface._allocate_request_id(), 65535)
        self.assertEqual(interface._allocate_request_id(), 1)

    def test_bounded_telemetry_queue_drops_oldest_frame(self):
        async def run_test():
            interface = SerialInterface()

            for uptime in range(1, 10):
                interface._enqueue_telemetry({"uptime": uptime})

            retained = [frame["uptime"] for frame in interface._telemetry_frames]
            self.assertEqual(retained, [9])
            self.assertEqual(
                list(interface._firmware_events),
                [{"fw_error": "TELEMETRY_FRAME_DROPPED"}],
            )

        asyncio.run(run_test())

    def test_telemetry_pressure_cannot_evict_firmware_error(self):
        interface = SerialInterface()
        firmware_error = {"fw_error": "THERMAL_SAFETY_LIMIT_EXCEEDED"}

        interface._enqueue_firmware_event(firmware_error)
        for uptime in range(20):
            interface._enqueue_telemetry({"uptime": uptime})

        self.assertEqual(interface._firmware_events[0], firmware_error)
        self.assertIn(
            {"fw_error": "TELEMETRY_FRAME_DROPPED"},
            interface._firmware_events,
        )
        self.assertEqual(len(interface._telemetry_frames), 1)

    def test_firmware_event_overflow_fails_transport_closed(self):
        interface = SerialInterface()
        reader = QueueReader()
        writer = ReactiveWriter(reader)
        interface.writer = writer
        interface.running = True

        for index in range(9):
            interface._enqueue_firmware_event({"fw_error": f"FAULT_{index}"})

        self.assertFalse(interface.running)
        self.assertTrue(writer.closed)
        self.assertTrue(interface._firmware_event_overflow)
        self.assertEqual(len(interface._firmware_events), 8)

    def test_reconnect_discards_old_telemetry_but_preserves_firmware_error(self):
        async def run_test():
            interface = SerialInterface()
            callback_started = asyncio.Event()
            hold_callback = asyncio.Event()
            processed = []

            async def callback(frame):
                callback_started.set()
                await hold_callback.wait()
                processed.append(frame)

            interface.set_telemetry_callback(callback)
            interface._start_telemetry_worker()
            interface._enqueue_telemetry({"uptime": 1, "state": 0})
            await callback_started.wait()
            interface._enqueue_telemetry({"uptime": 2, "state": 0})
            firmware_error = {"fw_error": "WATCHDOG_HEARTBEAT_TIMEOUT"}
            interface._enqueue_firmware_event(firmware_error)
            interface.set_telemetry_callback(None)

            await interface._reset_telemetry_worker_for_reconnect()

            self.assertEqual(list(interface._telemetry_frames), [])
            self.assertEqual(list(interface._firmware_events), [firmware_error])
            self.assertEqual(processed, [])
            await interface._stop_telemetry_worker()

        asyncio.run(run_test())

    def test_matching_ack_and_nack_control_result(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(
                reader, decision=lambda command: command.get("state") == 0
            )
            interface.reader = reader
            interface.writer = writer
            interface.running = True
            read_task = asyncio.create_task(interface._read_loop(reader))

            accepted = await interface.send_command(
                {"cmd": "SET_STATE", "state": 0}, timeout=0.5
            )
            rejected = await interface.send_command(
                {"cmd": "SET_STATE", "state": 1}, timeout=0.5
            )

            self.assertTrue(accepted)
            self.assertFalse(rejected)
            sent = [json.loads(payload) for payload in writer.payloads]
            self.assertEqual([command["rid"] for command in sent], [1, 2])
            self.assertTrue(
                all(1 <= command["rid"] <= 65535 for command in sent)
            )

            reader.lines.put_nowait(b"")
            await read_task

        asyncio.run(run_test())

    def test_command_name_mismatch_aborts_transport(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(reader)
            interface.writer = writer
            interface.running = True
            future = asyncio.get_running_loop().create_future()
            interface._pending_acks[7] = ("SET_STATE", future)

            interface._resolve_command_response(
                {"ack": True, "rid": 7, "cmd": "SET_TEMP"}
            )

            self.assertFalse(await future)
            self.assertFalse(interface.running)
            self.assertTrue(writer.closed)
            self.assertEqual(
                list(interface._firmware_events),
                [{"fw_error": "ACK_CORRELATION_FAILURE"}],
            )

        asyncio.run(run_test())

    def test_malformed_ack_aborts_transport_and_fails_pending(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(reader)
            interface.writer = writer
            interface.running = True
            future = asyncio.get_running_loop().create_future()
            interface._pending_acks[8] = ("SET_TEMP", future)

            interface._resolve_command_response(
                {"ack": "yes", "rid": 8, "cmd": "SET_TEMP"}
            )

            self.assertFalse(await future)
            self.assertFalse(interface.running)
            self.assertTrue(writer.closed)
            self.assertEqual(
                list(interface._firmware_events),
                [{"fw_error": "ACK_CORRELATION_FAILURE"}],
            )

        asyncio.run(run_test())

    def test_ack_timeout_cleans_pending_request(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            interface.reader = reader
            interface.writer = ReactiveWriter(reader)
            interface.running = True

            accepted = await interface.send_command(
                {"cmd": "SET_TEMP", "zone": 0, "val": 50.0}, timeout=0.01
            )

            self.assertFalse(accepted)
            self.assertEqual(interface._pending_acks, {})

        asyncio.run(run_test())

    def test_disconnect_fails_pending_request(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(reader)
            interface.reader = reader
            interface.writer = writer
            interface.running = True

            command_task = asyncio.create_task(
                interface.send_command(
                    {"cmd": "TARE_LOADCELL"}, timeout=1.0
                )
            )
            while not writer.payloads:
                await asyncio.sleep(0)
            await interface.disconnect()

            self.assertFalse(await command_task)
            self.assertTrue(writer.closed)
            self.assertEqual(interface._pending_acks, {})

        asyncio.run(run_test())

    def test_cancellation_during_write_cleans_pending_request(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = BlockingWriter(reader)
            interface.reader = reader
            interface.writer = writer
            interface.running = True

            command_task = asyncio.create_task(
                interface.send_command({"cmd": "SET_STATE", "state": 0})
            )
            await writer.drain_started.wait()
            command_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await command_task
            self.assertEqual(interface._pending_acks, {})
            self.assertFalse(interface.running)
            self.assertTrue(writer.closed)

        asyncio.run(run_test())

    def test_write_timeout_aborts_transport_and_releases_lock(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = BlockingWriter(reader)
            interface.reader = reader
            interface.writer = writer
            interface.running = True

            accepted = await interface.send_command(
                {"cmd": "SET_STATE", "state": 0}, timeout=0.01
            )

            self.assertFalse(accepted)
            self.assertFalse(interface.running)
            self.assertTrue(writer.closed)
            self.assertFalse(interface._write_lock.locked())
            self.assertEqual(interface._pending_acks, {})

        asyncio.run(run_test())

    def test_callback_can_await_ack_without_blocking_reader(self):
        async def run_test():
            interface = SerialInterface()
            reader = QueueReader()
            writer = ReactiveWriter(reader, decision=lambda _command: True)
            interface.reader = reader
            interface.writer = writer
            interface.running = True
            interface._start_telemetry_worker()

            callback_order = []
            first_callback_started = asyncio.Event()
            callbacks_done = asyncio.Event()

            async def callback(frame):
                callback_order.append(frame["uptime"])
                if frame["uptime"] == 1:
                    first_callback_started.set()
                    self.assertTrue(
                        await interface.send_command(
                            {"cmd": "SET_STATE", "state": 0}, timeout=0.5
                        )
                    )
                if len(callback_order) == 2:
                    callbacks_done.set()

            interface.set_telemetry_callback(callback)
            read_task = asyncio.create_task(interface._read_loop(reader))
            reader.lines.put_nowait(b'{"uptime":1,"state":0}\n')
            await asyncio.wait_for(first_callback_started.wait(), timeout=1.0)
            reader.lines.put_nowait(b'{"uptime":2,"state":0}\n')

            await asyncio.wait_for(callbacks_done.wait(), timeout=1.0)
            self.assertEqual(callback_order, [1, 2])

            reader.lines.put_nowait(b"")
            await read_task
            await interface._stop_telemetry_worker()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
