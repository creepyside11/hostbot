"""Trusted-code subprocess runner. A virtualenv is dependency isolation, not security isolation."""
import base64
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decrypt(payload, key):
    raw = base64.b64decode(payload, validate=True)
    return json.loads(AESGCM(bytes.fromhex(key)).decrypt(raw[:12], raw[12:], None))


def extract_zip(raw, destination):
    """Reject traversal, symlinks, bombs and oversized sources before extracting anything."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
            raise ValueError('Архив: максимум 2000 файлов и 50 МБ после распаковки.')
        for info in entries:
            name = info.filename
            path = Path(name)
            mode = info.external_attr >> 16
            if '\\' in name or path.is_absolute() or '..' in path.parts or ':' in name or stat.S_ISLNK(mode):
                raise ValueError('Архив содержит недопустимые пути или символьные ссылки.')
            if not (destination / path).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Недопустимый путь в архиве.')
            if info.flag_bits & 1:
                raise ValueError('Архивы с паролем не поддерживаются.')
        for info in entries:
            path = Path(info.filename)
            if any(p in {'.git', '.venv', 'venv', 'node_modules', '__MACOSX'} or p == '.env' or p.startswith('.env.') for p in path.parts):
                continue
            target = destination / path
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
    children = list(destination.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return destination


def redact(text, secrets):
    for value in sorted((str(v) for v in secrets.values() if v), key=len, reverse=True):
        text = text.replace(value, '[СКРЫТО]')
    text = re.sub(r'\b\d{5,}:[A-Za-z0-9_-]{20,}\b', '[BOT_TOKEN СКРЫТ]', text)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).replace('\x00', '')


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Also remove descendants even if the group leader already exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


class Runner:
    def __init__(self, root, log):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.processes = {}
        self.builds = {}
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.install_timeout = int(os.getenv('INSTALL_TIMEOUT', '600'))

    def environment(self, home, secrets=None):
        # Do not inherit the manager's BOT_TOKEN, DATABASE_URL, or encryption key.
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
               'PYTHONUNBUFFERED': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_CACHE_DIR': '1',
               'GIT_TERMINAL_PROMPT': '0', 'TMPDIR': str(home / 'tmp')}
        (home / 'tmp').mkdir(parents=True, exist_ok=True)
        if secrets:
            env.update(secrets)
        return env

    def reader(self, bot_id, process, secrets):
        window, count = time.monotonic(), 0
        try:
            while True:
                line = process.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    while line and not line.endswith(b'\n'):
                        line = process.stdout.readline(65537)
                    text = '[Слишком длинная строка пропущена]'
                else:
                    text = redact(line.decode('utf-8', errors='replace').rstrip(), secrets)
                if time.monotonic() - window >= 1:
                    count, window = 0, time.monotonic()
                if count < 20 and text:
                    self.log(bot_id, text[:4000])
                elif count == 20:
                    self.log(bot_id, '[Частые сообщения ограничены до 20 строк/сек]')
                count += 1
        finally:
            process.stdout.close()

    def spawn(self, bot_id, command, cwd, env, secrets, build=False):
        with self.lock:
            if self.closing.is_set():
                raise RuntimeError('Исполнитель завершается.')
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            (self.builds if build else self.processes)[bot_id] = process
        thread = threading.Thread(target=self.reader, args=(bot_id, process, secrets), daemon=True)
        thread.start()
        return process, thread

    def install_command(self, bot_id, command, cwd, env, secrets):
        process, reader = self.spawn(bot_id, command, cwd, env, secrets, build=True)
        try:
            result = process.wait(timeout=self.install_timeout)
            reader.join(timeout=5)
            if result != 0:
                raise RuntimeError('Установка зависимостей завершилась ошибкой. Подробности выше в логах.')
        except subprocess.TimeoutExpired:
            raise RuntimeError('Превышено время установки зависимостей.') from None
        finally:
            kill_group(process)
            with self.lock:
                self.builds.pop(bot_id, None)

    def prepare(self, bot, secrets):
        bot_id = str(bot['id'])
        home = self.root / bot_id
        source = home / 'source'
        staging = home / 'staging'
        home.mkdir(exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        self.log(bot_id, 'Получение исходного кода…')
        try:
            if bot['source'] == 'github':
                repo = bot['repo']
                if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
                    raise ValueError('Недопустимая ссылка GitHub.')
                slug = repo.removeprefix('https://github.com/')
                url = f'https://codeload.github.com/{slug}/zip/refs/heads/{urllib.parse.quote(bot["branch"], safe="")}'
                request = urllib.request.Request(url, headers={'User-Agent': 'EmeraldHost/1.0'})
                deadline = time.monotonic() + 90
                with urllib.request.urlopen(request, timeout=20) as response:
                    chunks, length = [], 0
                    while chunk := response.read(65536):
                        length += len(chunk)
                        if length > 20 * 1024 * 1024:
                            raise ValueError('GitHub-архив больше 20 МБ.')
                        if time.monotonic() > deadline or self.closing.is_set():
                            raise ValueError('Получение кода прервано по таймауту.')
                        chunks.append(chunk)
                raw = b''.join(chunks)
            else:
                raw = bytes(bot['archive'])
            project = extract_zip(raw, staging)
            target = (project / bot['entrypoint']).resolve()
            if not target.is_relative_to(project.resolve()) or not target.is_file():
                raise ValueError('Файл запуска не найден. Проверьте путь относительно корня проекта.')
            shutil.rmtree(source, ignore_errors=True)
            project.rename(source)
            if staging.exists():
                shutil.rmtree(staging)
            env = self.environment(home)
            if bot['runtime'] == 'python':
                venv = home / '.venv'
                shutil.rmtree(venv, ignore_errors=True)
                self.log(bot_id, 'Создание отдельного Python-окружения…')
                self.install_command(bot_id, [sys.executable, '-m', 'venv', str(venv)], source, env, secrets)
                python = str(venv / 'bin/python')
                requirements = source / 'requirements.txt'
                if requirements.is_file():
                    self.log(bot_id, 'Установка зависимостей из requirements.txt…')
                    self.install_command(bot_id, [python, '-m', 'pip', 'install', '--no-cache-dir', '-r', 'requirements.txt'], source, env, secrets)
                else:
                    self.log(bot_id, 'requirements.txt отсутствует — установка пропущена.')
            elif bot['runtime'] == 'node':
                if not shutil.which('node') or not shutil.which('npm'):
                    raise RuntimeError('В окружении исполнителя нет Node.js/npm. Используйте Dockerfile проекта.')
                if (source / 'package.json').is_file():
                    self.log(bot_id, 'Установка зависимостей из package.json…')
                    command = ['npm', 'ci' if (source / 'package-lock.json').is_file() else 'install', '--omit=dev', '--no-audit', '--no-fund']
                    self.install_command(bot_id, command, source, env, secrets)
            (home / 'ready').write_text('1')
            self.log(bot_id, 'Код и зависимости готовы.')
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def start(self, bot, secrets, rebuild=False):
        bot_id = str(bot['id'])
        self.stop(bot_id)
        home = self.root / bot_id
        if rebuild or not (home / 'ready').is_file():
            (home / 'ready').unlink(missing_ok=True)
            self.prepare(bot, secrets)
        with self.lock:
            if len(self.processes) >= int(os.getenv('MAX_RUNNING_BOTS', '10')):
                raise RuntimeError('Достигнут лимит одновременно запущенных ботов.')
        source = home / 'source'
        env = self.environment(home, secrets)
        if bot['runtime'] == 'python':
            env['VIRTUAL_ENV'] = str(home / '.venv')
            env['PATH'] = str(home / '.venv/bin') + ':' + env['PATH']
            command = [str(home / '.venv/bin/python'), '-u', bot['entrypoint']]
        else:
            command = ['node', bot['entrypoint']]
        self.log(bot_id, 'Запуск процесса…')
        process, _ = self.spawn(bot_id, command, source, env, secrets)
        if self.closing.wait(3):
            raise RuntimeError('Исполнитель завершается.')
        if process.poll() is not None:
            self.stop(bot_id)
            raise RuntimeError('Бот завершился сразу после запуска. Подробности выше в логах.')
        self.log(bot_id, 'Процесс работает. Telegram-подключение проверяйте по логам самого бота.')

    def stop(self, bot_id):
        with self.lock:
            process = self.processes.pop(bot_id, None)
        if process:
            kill_group(process)

    def delete(self, bot_id):
        self.stop(bot_id)
        shutil.rmtree(self.root / bot_id, ignore_errors=True)

    def exited(self, exclude=None):
        results = []
        with self.lock:
            for bot_id, process in list(self.processes.items()):
                if bot_id != exclude and process.poll() is not None:
                    results.append((bot_id, process.returncode))
                    kill_group(process)
                    self.processes.pop(bot_id)
        return results

    def shutdown(self):
        self.closing.set()
        with self.lock:
            processes = list(self.processes.values()) + list(self.builds.values())
            self.processes.clear()
            self.builds.clear()
        for process in processes:
            kill_group(process)
