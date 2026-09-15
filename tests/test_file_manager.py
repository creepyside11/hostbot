import base64
import tempfile
import unittest
from pathlib import Path

import file_manager


class FileManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = file_manager.ROOT
        file_manager.ROOT = Path(self.tmp.name).resolve()
        self.bot_id = '11111111-1111-1111-1111-111111111111'
        self.source = file_manager.ROOT / self.bot_id / 'source'
        self.source.mkdir(parents=True)
        (file_manager.ROOT / self.bot_id / 'ready').write_text('1')

    def tearDown(self):
        file_manager.ROOT = self.old_root
        self.tmp.cleanup()

    def request(self, operation, path='', target=None, payload=None):
        return file_manager.execute_request({
            'operation': operation,
            'bot_id': self.bot_id,
            'file_path': path,
            'target_path': target,
            'payload': payload,
        })

    def test_create_read_rename_list_and_delete(self):
        self.request('mkdir', 'src')
        self.request('write', 'src/main.py', payload='print("hello")\n')
        read = self.request('read', 'src/main.py')
        self.assertEqual(read['content'], 'print("hello")\n')
        rows = self.request('list', 'src')['entries']
        self.assertEqual([(x['name'], x['type']) for x in rows], [('main.py', 'file')])
        self.request('rename', 'src/main.py', target='src/bot.py')
        self.assertTrue((self.source / 'src' / 'bot.py').is_file())
        self.request('delete', 'src/bot.py')
        self.request('delete', 'src')
        self.assertFalse((self.source / 'src').exists())

    def test_upload_binary_but_editor_rejects_binary(self):
        payload = base64.b64encode(b'\x00\x01\x02PNG').decode()
        result = self.request('upload', 'asset.bin', payload=payload)
        self.assertEqual(result['size'], 6)
        with self.assertRaisesRegex(ValueError, 'Бинарный'):
            self.request('read', 'asset.bin')

    def test_blocks_traversal_internal_paths_and_symlinks(self):
        for path in ('../escape.py', '.venv/bin/python', '.env', 'node_modules/x.js'):
            with self.assertRaises(ValueError, msg=path):
                self.request('write', path, payload='bad')
        outside = file_manager.ROOT / 'outside'
        outside.mkdir()
        (self.source / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'пределы|Символьные'):
            self.request('write', 'link/escape.txt', payload='bad')
        self.assertFalse((outside / 'escape.txt').exists())

    def test_text_and_upload_limits(self):
        with self.assertRaisesRegex(ValueError, '512 КБ'):
            self.request('write', 'large.txt', payload='a' * (file_manager.MAX_TEXT_BYTES + 1))
        payload = base64.b64encode(b'x' * (file_manager.MAX_UPLOAD_BYTES + 1)).decode()
        with self.assertRaisesRegex(ValueError, '2 МБ'):
            self.request('upload', 'large.bin', payload=payload)


if __name__ == '__main__':
    unittest.main()
