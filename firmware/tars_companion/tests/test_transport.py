import sys
import types
import unittest
from unittest.mock import patch

from tars_companion.transport import SerialTransport


class FakeSerial:
    def __init__(self):
        self.port = None
        self.baudrate = None
        self.timeout = None
        self.dtr = True
        self.rts = True
        self.open_snapshot = None

    def open(self):
        self.open_snapshot = {
            "port": self.port,
            "baudrate": self.baudrate,
            "timeout": self.timeout,
            "dtr": self.dtr,
            "rts": self.rts,
        }


class SerialTransportTests(unittest.TestCase):
    def test_disables_modem_control_before_opening_ch340(self):
        fake_module = types.SimpleNamespace(Serial=FakeSerial)
        with patch.dict(sys.modules, {"serial": fake_module}):
            transport = SerialTransport("COM7", 115200)

        self.assertEqual(
            transport._serial.open_snapshot,
            {
                "port": "COM7",
                "baudrate": 115200,
                "timeout": 0.25,
                "dtr": False,
                "rts": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
