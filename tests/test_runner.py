import base64
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import Runner, extract_zip, redact, decrypt, resolve_entrypoint


def archive(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, body in files.items():
            z.writestr(name, body)
    return buf.getvalue()


class RunnerTests(unittest.TestCase):
    def test_main_file_fallback_and_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'bot.py').write_text('pass')
            self.assertEqual(resolve_entrypoint(root,'main.py','python'),'bot.py')
            (root/'main.py').write_text('pass')
            self.assertEqual(resolve_entrypoint(root,'main.py','python'),'main.py')
            self.assertEqual(resolve_entrypoint(root,'bot.py','python'),'bot.py')
            with self.assertRaises(ValueError):resolve_entrypoint(root,'other.py','python')
            with self.assertRaises(ValueError):resolve_entrypoint(root,'../escape.py','python')
            with self.assertRaises(ValueError):resolve_entrypoint(root,'index.js','node')

    def test_dockerfile_rejected_without_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=Runner(tmp,lambda *_:None)
            runner.docker=None
            bot={'id':'33333333-3333-3333-3333-333333333333','source':'zip','runtime':'python','entrypoint':'main.py',
                 'build_mode':'dockerfile','dockerfile_path':'Dockerfile',
                 'archive':archive({'Dockerfile':'FROM python:3.11-slim\nCMD ["python", "bot.py"]','bot.py':'print(1)'})}
            try:
                with self.assertRaisesRegex(RuntimeError,'Docker-'):
                    runner.start(bot,{})
                self.assertFalse(runner.processes)
            finally:runner.shutdown()

    def test_reject_traversal_symlink_and_large_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ['../escape.py', '/absolute.py', 'x/../../escape.py', 'x\\escape.py']:
                with self.assertRaises(ValueError):
                    extract_zip(archive({name:'bad'}), Path(tmp))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                link = zipfile.ZipInfo('link')
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(link, '/etc/passwd')
            with self.assertRaises(ValueError):
                extract_zip(buf.getvalue(), Path(tmp))
            with self.assertRaises(ValueError):
                extract_zip(archive({f'{i}.py':'' for i in range(2001)}),Path(tmp))

    def test_flatten_folder_and_exclude_local_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = extract_zip(archive({'repo/main.py':'print(1)', 'repo/.env':'secret', 'repo/.venv/x':'ignore'}), Path(tmp))
            self.assertEqual(root.name, 'repo')
            self.assertTrue((root / 'main.py').exists())
            self.assertFalse((root / '.env').exists())

    def test_node_python_encryption_compatibility(self):
        script = "import {encrypt} from './lib/security.js'; process.env.ENCRYPTION_KEY='12'.repeat(32); console.log(encrypt({BOT_TOKEN:'test-secret'}))"
        result = subprocess.check_output(['node', '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[2]/'hostsait', text=True)
        self.assertEqual(decrypt(result.strip(), '12'*32), {'BOT_TOKEN':'test-secret'})
        raw=bytearray(base64.b64decode(result));raw[-1]^=1
        with self.assertRaises(Exception):decrypt(base64.b64encode(raw).decode(),'12'*32)

    def test_secrets_not_inherited_or_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runner(tmp,lambda *_:None)
            os.environ['DATABASE_URL']='manager-secret'
            env=r.environment(Path(tmp),{'BOT_TOKEN':'child-secret'})
            self.assertNotIn('DATABASE_URL',env)
            self.assertEqual(env['BOT_TOKEN'],'child-secret')
            bot_env=r.environment(Path(tmp),{'BOT_TOKEN':'child-secret','DATABASE_URL':'postgresql://child:pass@host:5432/botdb'})
            self.assertEqual(bot_env['DATABASE_URL'],'postgresql://child:pass@host:5432/botdb')
            self.assertEqual(redact('token=abc123',{'KEY':'abc123'}),'token=[СКРЫТО]')

    def test_install_local_requirement_start_stop_restart(self):
        # Offline real pip installation into the bot virtualenv, followed by a long-lived process.
        with tempfile.TemporaryDirectory() as tmp:
            events=[]
            r=Runner(tmp,lambda _,msg,stream="runtime":events.append((stream,msg)))
            wheel=archive({'emerald_sample.py':'VALUE="installed"',
                'emerald_sample-1.0.dist-info/METADATA':'Metadata-Version: 2.1\nName: emerald-sample\nVersion: 1.0\n',
                'emerald_sample-1.0.dist-info/WHEEL':'Wheel-Version: 1.0\nGenerator: emerald-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                'emerald_sample-1.0.dist-info/RECORD':''})
            bot={'id':'11111111-1111-1111-1111-111111111111','source':'zip','runtime':'python','entrypoint':'main.py',
                 'archive':archive({'main.py':'import emerald_sample,os,time\nprint(emerald_sample.VALUE,os.getenv("BOT_TOKEN"),flush=True)\ntime.sleep(120)',
                    'requirements.txt':'./emerald_sample-1.0-py3-none-any.whl\n',
                    'emerald_sample-1.0-py3-none-any.whl':wheel})}
            try:
                r.start(bot,{'BOT_TOKEN':'child-secret'})
                first=r.processes[bot['id']]
                self.assertIsNone(first.poll())
                self.assertTrue(any(stream=='build' and 'requirements.txt' in x for stream,x in events))
                self.assertTrue(any(stream=='runtime' and 'installed [СКРЫТО]' in x for stream,x in events))
                r.stop(bot['id']);self.assertIsNotNone(first.poll())
                r.start(bot,{'BOT_TOKEN':'child-secret'})
                self.assertNotEqual(first.pid,r.processes[bot['id']].pid)
                r.delete(bot['id']);self.assertFalse((Path(tmp)/bot['id']).exists())
            finally:r.shutdown()

    def test_dependency_failure_does_not_start_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=Runner(tmp,lambda *_:None)
            bot={'id':'22222222-2222-2222-2222-222222222222','source':'zip','runtime':'python','entrypoint':'main.py',
                 'archive':archive({'main.py':'print("must not run")','requirements.txt':'./nonexistent-package'})}
            try:
                with self.assertRaises(RuntimeError):r.start(bot,{})
                self.assertNotIn(bot['id'],r.processes)
                self.assertFalse((Path(tmp)/bot['id']/'ready').exists())
            finally:r.shutdown()

if __name__=='__main__': unittest.main()
