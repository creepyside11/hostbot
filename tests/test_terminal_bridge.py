import os
import queue
import tempfile
import time
import unittest
from pathlib import Path

from terminal_bridge import TerminalBridge


class StubRunner:
    def __init__(self, root):
        self.root = Path(root)
        self.docker = None

    def environment(self, home, secrets=None):
        env = {
            'PATH': '/usr/local/bin:/usr/bin:/bin',
            'HOME': str(home),
            'LANG': 'C.UTF-8',
            'TMPDIR': str(home / 'tmp'),
        }
        (home / 'tmp').mkdir(parents=True, exist_ok=True)
        if secrets:
            env.update(secrets)
        return env


class TerminalBridgeTests(unittest.TestCase):
    def test_system_bot_gets_project_pty_without_manager_database_secret(self):
        old_database = os.environ.get('DATABASE_URL')
        os.environ['DATABASE_URL'] = 'manager-secret-must-not-leak'
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bot_id = '11111111-1111-1111-1111-111111111111'
                home = Path(tmp) / bot_id
                (home / 'source').mkdir(parents=True)
                (home / 'ready').write_text('1')
                bridge = TerminalBridge(StubRunner(tmp))
                try:
                    backend = bridge.open(
                        'session-1',
                        {'id': bot_id, 'build_mode': 'system', 'runtime': 'node'},
                        False,
                        {'BOT_TOKEN': 'child-project-secret'},
                    )
                    self.assertEqual(backend, 'system')
                    bridge.send(
                        'session-1',
                        "printf 'RESULT_DB=<%s> RESULT_TOKEN=<%s>\\n' \"$DATABASE_URL\" \"$BOT_TOKEN\"\n",
                    )
                    text = ''
                    deadline = time.time() + 3
                    while time.time() < deadline and 'RESULT_DB=<>' not in text:
                        try:
                            text += bridge.outputs.get(timeout=.2)[2]
                        except queue.Empty:
                            pass
                    self.assertNotIn('manager-secret-must-not-leak', text)
                    self.assertNotIn('child-project-secret', text)
                    self.assertIn('RESULT_DB=<> RESULT_TOKEN=<[СКРЫТО]>', text)
                finally:
                    bridge.shutdown()
        finally:
            if old_database is None:
                os.environ.pop('DATABASE_URL', None)
            else:
                os.environ['DATABASE_URL'] = old_database

    def test_unprepared_system_bot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = TerminalBridge(StubRunner(tmp))
            try:
                with self.assertRaisesRegex(RuntimeError, 'не подготовлен'):
                    bridge.open(
                        'session-2',
                        {'id': '22222222-2222-2222-2222-222222222222', 'build_mode': 'system', 'runtime': 'node'},
                        False,
                        {},
                    )
            finally:
                bridge.shutdown()

    def test_opening_session_query_keeps_terminal_session_id_separate_from_bot_id(self):
        source = (Path(__file__).resolve().parents[1] / 'terminal_bridge.py').read_text(encoding='utf-8')
        self.assertIn('s.id AS session_id', source)
        self.assertIn("str(row['session_id'])", source)
        self.assertNotIn('SELECT s.id,s.bot_id,s.setup_mode,b.id', source)


if __name__ == '__main__':
    unittest.main()
