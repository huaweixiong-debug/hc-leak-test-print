import unittest
import threading
import time
from unittest import mock

import modbus_utils


class _FailingSerial:
    def __init__(self, error):
        self.error = error
        self.is_open = True
        self.write_calls = 0
        self.closed = False

    def write(self, _data):
        self.write_calls += 1
        raise self.error

    def close(self):
        self.closed = True
        self.is_open = False


class ModbusRealtimeCacheTests(unittest.TestCase):
    def setUp(self):
        modbus_utils._realtime_cache_response = None
        modbus_utils._realtime_cache_at = 0.0
        modbus_utils._serial_cooldown_until = 0.0
        modbus_utils._serial_connection = None

    def tearDown(self):
        modbus_utils._close_serial_connection()
        modbus_utils._realtime_cache_response = None
        modbus_utils._realtime_cache_at = 0.0
        modbus_utils._serial_cooldown_until = 0.0

    def test_realtime_reads_share_one_serial_response_inside_cache_window(self):
        response = "FF031A" + ("0000" * 13) + "0000"

        with (
            mock.patch.object(modbus_utils, "TRANSPORT", "serial"),
            mock.patch.object(
                modbus_utils, "_send_raw_serial", return_value=response
            ) as send_serial,
            mock.patch.object(
                modbus_utils.time, "monotonic", side_effect=[10.0, 10.0, 10.1]
            ),
        ):
            first = modbus_utils.read_holding_registers(0x30, 13)
            second = modbus_utils.read_holding_registers(0x30, 13)

        self.assertEqual(response, first)
        self.assertEqual(response, second)
        send_serial.assert_called_once()

    def test_winerror31_closes_handle_and_enters_cooldown_without_retry(self):
        error = OSError("device is not functioning")
        error.winerror = 31
        serial_port = _FailingSerial(error)
        modbus_utils._serial_connection = serial_port

        with (
            mock.patch.object(
                modbus_utils.time, "monotonic", side_effect=[20.0, 20.0, 20.0]
            ),
            mock.patch.object(modbus_utils, "_log_serial_issue"),
        ):
            result = modbus_utils._send_raw_serial("FF030030000D91DE")

        self.assertIsNone(result)
        self.assertEqual(1, serial_port.write_calls)
        self.assertTrue(serial_port.closed)
        self.assertEqual(
            20.0 + modbus_utils.SERIAL_ERROR_COOLDOWN_SECONDS,
            modbus_utils._serial_cooldown_until,
        )

    def test_cooldown_skips_serial_open_and_io(self):
        modbus_utils._serial_cooldown_until = 31.0

        with (
            mock.patch.object(modbus_utils.time, "monotonic", return_value=30.0),
            mock.patch.object(modbus_utils, "_get_serial_connection") as get_serial,
        ):
            result = modbus_utils._send_raw_serial("FF030030000D91DE")

        self.assertIsNone(result)
        get_serial.assert_not_called()

    def test_stepcode_is_decoded_from_shared_realtime_block(self):
        registers = [0, 0, 0, 0, 0x0400] + [0] * 8
        payload = b"".join(value.to_bytes(2, "big") for value in registers)
        response = bytes([modbus_utils.STATION_ID, 0x03, len(payload)]) + payload

        self.assertEqual(
            4,
            modbus_utils.decode_realtime_step_code(response.hex()),
        )

    def test_transaction_blocks_other_reads_until_all_commands_finish(self):
        reader_started = threading.Event()
        reader_finished = threading.Event()

        def run_reader():
            reader_started.set()
            modbus_utils.read_holding_registers(0x20, 1)
            reader_finished.set()

        with mock.patch.object(
            modbus_utils, "_send_raw_serial", return_value="FF03020000A050"
        ):
            with modbus_utils.modbus_transaction():
                reader = threading.Thread(target=run_reader)
                reader.start()
                self.assertTrue(reader_started.wait(timeout=1))
                time.sleep(0.02)
                self.assertFalse(reader_finished.is_set())

            reader.join(timeout=1)

        self.assertTrue(reader_finished.is_set())


if __name__ == "__main__":
    unittest.main()
