"""Cloudflared process launch regression tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.tunnel.cloudflared_manager import CloudflaredManager


class CloudflaredManagerLaunchTests(unittest.TestCase):
    def test_cloudflared_console_is_hidden_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "cloudflared.exe"
            executable.write_bytes(b"fixture")
            process = mock.Mock()
            process.poll.return_value = None
            manager = CloudflaredManager(executable)

            with (
                mock.patch(
                    "app.tunnel.cloudflared_manager.subprocess.Popen",
                    return_value=process,
                ) as popen,
                mock.patch(
                    "app.tunnel.cloudflared_manager.threading.Thread.start"
                ),
            ):
                self.assertTrue(manager.start())

            args, kwargs = popen.call_args
            self.assertEqual(args[0][0], str(executable))
            self.assertFalse(kwargs["shell"])
            self.assertEqual(
                kwargs["creationflags"],
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )


if __name__ == "__main__":
    unittest.main()
