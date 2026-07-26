import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from label_data_files import LABEL_DATA_FILENAMES, write_label_data_files


class LabelDataFilesTests(unittest.TestCase):
    def test_writes_all_nine_label_fields(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            write_label_data_files(
                test_data={
                    "pressure1": 3.500,
                    "pressure1_unit": "bar",
                    "leak1": -0.0,
                    "leak1_unit": "cm³/min",
                    "pressure2": 8.1256,
                    "pressure2_unit": "kPa",
                    "leak2": None,
                    "leak2_unit": "Pa",
                },
                product_model="ALW-100",
                operator="张三",
                daily_sequence="0042",
                overall_result="pass",
                completed_at=datetime(2026, 7, 26, 14, 5, 9),
                target_dir=temporary_directory,
            )

            expected = {
                "product_model": "ALW-100\r\n",
                "completed_at": "2026-07-26 14:05:09\r\n",
                "daily_sequence": "0042\r\n",
                "pressure1": "3.5bar\r\n",
                "leak1": "0cm3/min\r\n",
                "pressure2": "8.126kPa\r\n",
                "leak2": "\r\n",
                "result": "PASS\r\n",
                "operator": "张三\r\n",
            }
            self.assertEqual(len(list(Path(temporary_directory).glob("*.txt"))), 9)
            for field_name, filename in LABEL_DATA_FILENAMES.items():
                content = (Path(temporary_directory) / filename).read_bytes().decode(
                    "utf-8-sig"
                )
                self.assertEqual(content, expected[field_name])

    def test_replaces_stale_second_program_values_with_empty_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            stale_pressure = Path(temporary_directory) / LABEL_DATA_FILENAMES["pressure2"]
            stale_leak = Path(temporary_directory) / LABEL_DATA_FILENAMES["leak2"]
            stale_pressure.write_text("old", encoding="utf-8")
            stale_leak.write_text("old", encoding="utf-8")

            write_label_data_files(
                test_data={"pressure1": 1, "leak1": 2},
                product_model="MODEL",
                operator="EMPLOYEE",
                daily_sequence="0001",
                overall_result="FAIL",
                completed_at=datetime(2026, 7, 26, 0, 0, 0),
                target_dir=temporary_directory,
            )

            self.assertEqual(stale_pressure.read_bytes().decode("utf-8-sig"), "\r\n")
            self.assertEqual(stale_leak.read_bytes().decode("utf-8-sig"), "\r\n")


if __name__ == "__main__":
    unittest.main()
