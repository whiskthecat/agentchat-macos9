#!/usr/bin/env python3
"""Compile complete native sender against recording fakes, with no inference."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class ApplicationSessionChecks(unittest.TestCase):
    def test_complete_sender_recording_scenarios(self):
        with tempfile.TemporaryDirectory(prefix='agentchat-ae-recording-') as directory:
            output = Path(directory) / 'checks'
            command = ['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', '-Wno-multichar',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                       '-I', str(HERE / 'tests/remote_applications'), '-I', str(HERE),
                       str(HERE / 'RemoteApplications.c'),
                       str(HERE / 'tests/remote_applications/recording_checks.c'), '-o', str(output)]
            subprocess.run(command, check=True, capture_output=True, text=True)
            environment = dict(os.environ, AGENT_TEST_MODE='1', UBSAN_OPTIONS='halt_on_error=1')
            result = subprocess.run([str(output)], env=environment, check=True,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.stdout,
                             '17 native sender recording scenarios passed; no Toolbox or inference used\n')


if __name__ == '__main__':
    unittest.main()
