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
    if hasattr(process, 'container'):
        process.stop()
        return
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


def resolve_entrypoint(project, requested, runtime):
    requested = requested or ('main.py' if runtime == 'python' else 'index.js')
    candidates = [requested]
    if runtime == 'python' and requested == 'main.py':
        candidates.append('bot.py')
    for candidate in candidates:
        target = (project / candidate).resolve()
        if not target.is_relative_to(project.resolve()):
            raise ValueError('Файл запуска должен находиться внутри проекта.')
        if target.is_file():
            return candidate
    raise ValueError('Файл запуска не найден: ' + ', '.join(candidates) + '. Измените его в настройках бота.')


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
        self.docker = None
        self.docker_bots = set()
        if os.getenv('DOCKER_HOST'):
            from docker_backend import DockerBackend
            try:
                self.docker = DockerBackend()
            except Exception:
                print('Docker-сборщик недоступен. Проверьте DOCKER_HOST и TLS. Системный режим доступен.', flush=True)

    def environment(self, home, secrets=None):
        # Do not inherit the manager's BOT_TOKEN, DATABASE_URL, or encryption key.
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
               'PYTHONUNBUFFERED': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_CACHE_DIR': '1',
               'GIT_TERMINAL_PROMPT': '0', 'TMPDIR': str(home / 'tmp')}
        (home / 'tmp').mkdir(parents=True, exist_ok=True)
        if secrets:
            env.update(secrets)
        return env

    def reader(self, bot_id, process, secrets, stream="runtime"):
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
                    self.log(bot_id, text[:4000], stream)
                elif count == 20:
                    self.log(bot_id, '[Частые сообщения ограничены до 20 строк/сек]', stream)
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
        thread = threading.Thread(target=self.reader, args=(bot_id, process, secrets, "build" if build else "runtime"), daemon=True)
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
        self.log(bot_id, 'Получение исходного кода…', 'build')
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
            custom = bot.get('build_mode') == 'dockerfile'
            if custom:
                if not self.docker:
                    raise RuntimeError('Docker-сборщик не подключён. На Bothost используйте системное окружение либо подключите внешний Docker-сервер.')
                target=(project / bot['dockerfile_path']).resolve()
                if not target.is_relative_to(project.resolve()) or not target.is_file():
                    raise ValueError('Dockerfile не найден по указанному пути.')
            else:
                resolved = resolve_entrypoint(project, bot['entrypoint'], bot['runtime'])
                self.log(bot_id, 'Главный файл: ' + resolved, 'build')
            shutil.rmtree(source, ignore_errors=True)
            project.rename(source)
            if staging.exists():
                shutil.rmtree(staging)
            env = self.environment(home)
            if custom:
                self.log(bot_id, 'Сборка из ' + bot['dockerfile_path'], 'build')
                self.docker.build(bot_id,source,bot['dockerfile_path'],
                                  lambda line:self.log(bot_id,redact(line,secrets),'build'),self.install_timeout,self.closing)
            elif bot['runtime'] == 'python':
                venv = home / '.venv'
                shutil.rmtree(venv, ignore_errors=True)
                self.log(bot_id, 'Создание отдельного Python-окружения…', 'build')
                self.install_command(bot_id, [sys.executable, '-m', 'venv', str(venv)], source, env, secrets)
                python = str(venv / 'bin/python')
                requirements = source / 'requirements.txt'
                if requirements.is_file():
                    self.log(bot_id, 'Установка зависимостей из requirements.txt…', 'build')
                    self.install_command(bot_id, [python, '-m', 'pip', 'install', '--no-cache-dir', '-r', 'requirements.txt'], source, env, secrets)
                else:
                    self.log(bot_id, 'requirements.txt отсутствует — установка пропущена.', 'build')
            elif bot['runtime'] == 'node':
                if not shutil.which('node') or not shutil.which('npm'):
                    raise RuntimeError('В окружении исполнителя нет Node.js/npm. Используйте Dockerfile проекта.')
                if (source / 'package.json').is_file():
                    self.log(bot_id, 'Установка зависимостей из package.json…', 'build')
                    command = ['npm', 'ci' if (source / 'package-lock.json').is_file() else 'install', '--omit=dev', '--no-audit', '--no-fund']
                    self.install_command(bot_id, command, source, env, secrets)
            (home / 'ready').write_text('1')
            self.log(bot_id, 'Код и зависимости готовы.', 'build')
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def start(self, bot, secrets, rebuild=False):
        bot_id = str(bot['id'])
        if bot.get('build_mode')=='dockerfile': self.docker_bots.add(bot_id)
        self.stop(bot_id)
        home = self.root / bot_id
        if rebuild or not (home / 'ready').is_file():
            (home / 'ready').unlink(missing_ok=True)
            try:
                self.prepare(bot, secrets)
            except Exception as error:
                error.log_stream='build'
                raise
        with self.lock:
            if len(self.processes) >= int(os.getenv('MAX_RUNNING_BOTS', '10')):
                raise RuntimeError('Достигнут лимит одновременно запущенных ботов.')
        source = home / 'source'
        env = self.environment(home, secrets)
        custom = bot.get('build_mode') == 'dockerfile'
        resolved = None if custom else resolve_entrypoint(source, bot['entrypoint'], bot['runtime'])
        if custom:
            if not self.docker:
                raise RuntimeError('Docker-сборщик недоступен.')
            with self.lock:
                if self.closing.is_set(): raise RuntimeError('Исполнитель завершается.')
                process=self.docker.start(bot_id, secrets)
                self.processes[bot_id]=process
            threading.Thread(target=self.reader,args=(bot_id,process,secrets),daemon=True).start()
        elif bot['runtime'] == 'python':
            env['VIRTUAL_ENV'] = str(home / '.venv')
            env['PATH'] = str(home / '.venv/bin') + ':' + env['PATH']
            command = [str(home / '.venv/bin/python'), '-u', resolved]
        else:
            command = ['node', resolved]
        self.log(bot_id, 'Запуск процесса…')
        if not custom:
            process, _ = self.spawn(bot_id, command, source, env, secrets)
        if self.closing.wait(3):
            raise RuntimeError('Исполнитель завершается.')
        if process.poll() is not None:
            self.stop(bot_id)
            raise RuntimeError('Бот завершился сразу после запуска. Подробности выше в логах.')
        self.log(bot_id, 'Процесс работает. Telegram-подключение проверяйте по логам самого бота.')
        return resolved

    def stop(self, bot_id):
        with self.lock:
            process = self.processes.pop(bot_id, None)
        if process:
            kill_group(process)
        elif bot_id in self.docker_bots:
            if not self.docker: raise RuntimeError('Docker-сервер недоступен: невозможно подтвердить остановку контейнера.')
            self.docker.stop_existing(bot_id)

    def delete(self, bot_id):
        self.stop(bot_id)
        if bot_id in self.docker_bots:
            if not self.docker: raise RuntimeError('Docker-сервер недоступен. Удаление контейнера не подтверждено.')
            self.docker.delete(bot_id)
            self.docker_bots.discard(bot_id)
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
            try:
                kill_group(process)
            except Exception:
                print('Не удалось подтвердить остановку процесса/контейнера. Проверьте исполнитель и Docker-сервер.', flush=True)
