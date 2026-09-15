"""Database-backed PTY bridge for Docker and system-mode bot projects."""
import os
import pty
import queue
import re
import shutil
import signal
import subprocess
import threading
import time

from runner import decrypt

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
SETUP_MARKER = 'EMERALD_TEMPLATE_SETUP_OK'


class LocalPty:
    """Interactive shell attached only to one bot's project directory/environment."""
    def __init__(self, runner, bot, secrets):
        bot_id = str(bot['id'])
        home = runner.root / bot_id
        source = home / 'source'
        if not (home / 'ready').is_file() or not source.is_dir():
            raise RuntimeError('Проект ещё не подготовлен. Сначала завершите деплой бота.')
        env = runner.environment(home, secrets)
        if bot.get('runtime') == 'python':
            venv = home / '.venv'
            python = venv / 'bin' / 'python'
            if not python.is_file():
                raise RuntimeError('Python-окружение проекта не найдено. Выполните обновление бота.')
            env['VIRTUAL_ENV'] = str(venv)
            env['PATH'] = str(venv / 'bin') + ':' + env['PATH']
        env['TERM'] = 'xterm-256color'
        env['COLORTERM'] = 'truecolor'
        shell = shutil.which('bash', path=env['PATH']) or '/bin/sh'
        env['SHELL'] = shell
        env['PS1'] = 'emerald:\\w$ ' if shell.endswith('bash') else 'emerald$ '
        command = [shell, '--noprofile', '--norc'] if shell.endswith('bash') else [shell]
        master, slave = pty.openpty()
        try:
            self.process = subprocess.Popen(
                command, cwd=source, env=env, stdin=slave, stdout=slave, stderr=slave,
                close_fds=True, start_new_session=True,
            )
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.fd = master
        self.lock = threading.Lock()

    def recv(self, size):
        fd = self.fd
        if fd is None:
            return b''
        return os.read(fd, size)

    def sendall(self, data):
        view = memoryview(data)
        with self.lock:
            fd = self.fd
            if fd is None:
                raise RuntimeError('PTY закрыт.')
            while view:
                written = os.write(fd, view)
                view = view[written:]

    def close(self):
        with self.lock:
            fd, self.fd = self.fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        process = getattr(self, 'process', None)
        if not process or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


class TerminalBridge:
    def __init__(self, runner):
        self.runner = runner
        self.sessions = {}
        self.outputs = queue.Queue(maxsize=4000)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.available = os.name == 'posix'

    def _socket_io(self, output):
        raw = getattr(output, '_sock', output)
        return output, raw

    def _recv(self, output, raw):
        if hasattr(raw, 'recv'):
            return raw.recv(8192)
        if hasattr(output, 'read'):
            return output.read(8192)
        return b''

    def _send(self, output, raw, data):
        if hasattr(raw, 'sendall'):
            raw.sendall(data)
            return
        if hasattr(raw, 'send'):
            raw.send(data)
            return
        if hasattr(output, 'write'):
            output.write(data)
            if hasattr(output, 'flush'):
                output.flush()
            return
        raise RuntimeError('PTY не поддерживает stdin.')

    def _close_socket(self, session):
        seen = set()
        for obj in (session.get('raw'), session.get('output')):
            try:
                if obj and id(obj) not in seen and hasattr(obj, 'close'):
                    seen.add(id(obj))
                    obj.close()
            except Exception:
                pass

    def _reader(self, session_id):
        while not self.stopping.is_set():
            with self.lock:
                session = self.sessions.get(session_id)
            if not session:
                return
            try:
                data = self._recv(session['output'], session['raw'])
            except Exception:
                data = b''
            if not data:
                try:
                    self.outputs.put_nowait((session_id, session['bot_id'], '\n[Emerald] PTY закрыт.\n', False, True))
                except queue.Full:
                    pass
                return
            text = ANSI.sub('', data.decode('utf-8', errors='replace')).replace('\x00', '')
            configured = SETUP_MARKER in text
            text = text.replace(SETUP_MARKER, '\n[Emerald] Настройка шаблона сохранена. Теперь можно нажать «Запустить».')
            for secret in list(session['secrets']):
                if secret:
                    text = text.replace(secret, '[СКРЫТО]')
            if text:
                try:
                    self.outputs.put_nowait((session_id, session['bot_id'], text[:16000], configured, False))
                except queue.Full:
                    pass

    def open(self, session_id, bot, setup_mode, secrets):
        bot_id = str(bot['id'])
        custom = bot.get('build_mode') == 'dockerfile'
        if custom:
            if not self.runner.docker:
                raise RuntimeError('Docker-терминал недоступен.')
            result = self.runner.docker.open_terminal(bot_id)
            backend = 'container'
        else:
            if not self.available:
                raise RuntimeError('System PTY недоступен на этом исполнителе.')
            if setup_mode:
                raise RuntimeError('Первичная настройка шаблона требует Docker-окружение.')
            result = LocalPty(self.runner, bot, secrets)
            backend = 'system'
        output, raw = self._socket_io(result)
        initial_secrets = [str(value) for value in secrets.values() if value]
        session = {
            'bot_id': bot_id, 'output': output, 'raw': raw, 'setup_mode': setup_mode,
            'secrets': initial_secrets[-64:], 'backend': backend,
        }
        with self.lock:
            old = self.sessions.pop(session_id, None)
            if old:
                self._close_socket(old)
            self.sessions[session_id] = session
        threading.Thread(target=self._reader, args=(session_id,), daemon=True).start()
        if setup_mode:
            time.sleep(0.15)
            self.send(session_id, 'python /app/emerald_setup.py\n', secret=False)
        return backend

    def send(self, session_id, data, secret=False):
        with self.lock:
            session = self.sessions.get(session_id)
        if not session:
            raise RuntimeError('PTY не подключён.')
        value = str(data)
        if secret:
            clean = value.rstrip('\r\n')
            if clean:
                session['secrets'].append(clean)
                session['secrets'][:] = session['secrets'][-64:]
        self._send(session['output'], session['raw'], value.encode('utf-8'))

    def close(self, session_id):
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session:
            self._close_socket(session)

    def tick(self, conn):
        # Advertise PTY capability independently from Docker support.
        conn.execute("UPDATE worker_health SET supports_terminal=%s WHERE id='nl'", (self.available,))
        # Open at most two new sessions per tick.
        rows = conn.execute("""SELECT s.id,s.bot_id,s.setup_mode,b.id,b.build_mode,b.runtime,b.status,b.secrets
            FROM terminal_sessions s JOIN bots b ON b.id=s.bot_id
            WHERE s.state='opening' ORDER BY s.created_at LIMIT 2""").fetchall()
        for row in rows:
            sid, bot_id = str(row['id']), str(row['bot_id'])
            try:
                if row['status'] not in ('running', 'stopped'):
                    raise RuntimeError('Дождитесь завершения текущей операции с ботом.')
                secrets = decrypt(row['secrets'], os.environ['ENCRYPTION_KEY'])
                backend = self.open(sid, row, bool(row['setup_mode']), secrets)
                conn.execute("UPDATE terminal_sessions SET state='open',error_message=NULL,updated_at=now(),last_activity_at=now() WHERE id=%s", (sid,))
                label = 'контейнера' if backend == 'container' else 'окружения проекта'
                conn.execute("INSERT INTO terminal_outputs(session_id,data) VALUES(%s,%s)", (sid, f'[Emerald] PTY {label} подключён.\n'))
            except Exception as error:
                conn.execute("UPDATE terminal_sessions SET state='error',error_message=%s,updated_at=now() WHERE id=%s", (str(error)[:500], sid))

        for row in conn.execute("SELECT id FROM terminal_sessions WHERE state='closing' LIMIT 10").fetchall():
            sid = str(row['id'])
            self.close(sid)
            conn.execute("UPDATE terminal_sessions SET state='closed',updated_at=now() WHERE id=%s", (sid,))

        inputs = conn.execute("""SELECT i.id,i.session_id,i.data,i.is_secret
            FROM terminal_inputs i JOIN terminal_sessions s ON s.id=i.session_id
            WHERE i.state='pending' AND s.state='open' ORDER BY i.id LIMIT 40
            FOR UPDATE OF i SKIP LOCKED""").fetchall()
        for row in inputs:
            iid, sid = row['id'], str(row['session_id'])
            conn.execute("UPDATE terminal_inputs SET state='active' WHERE id=%s", (iid,))
            try:
                self.send(sid, row['data'], bool(row['is_secret']))
                # Secret terminal input is deliberately not retained after delivery.
                if row['is_secret']:
                    conn.execute('DELETE FROM terminal_inputs WHERE id=%s', (iid,))
                else:
                    conn.execute("UPDATE terminal_inputs SET state='done' WHERE id=%s", (iid,))
                conn.execute("UPDATE terminal_sessions SET last_activity_at=now(),updated_at=now() WHERE id=%s", (sid,))
            except Exception as error:
                conn.execute("UPDATE terminal_sessions SET state='error',error_message=%s,updated_at=now() WHERE id=%s", (str(error)[:500], sid))
                conn.execute('DELETE FROM terminal_inputs WHERE id=%s', (iid,))
                self.close(sid)

        for _ in range(300):
            try:
                sid, bot_id, text, configured, closed = self.outputs.get_nowait()
            except queue.Empty:
                break
            if text:
                conn.execute("INSERT INTO terminal_outputs(session_id,data) SELECT id,%s FROM terminal_sessions WHERE id=%s", (text, sid))
            if configured:
                conn.execute("UPDATE bots SET template_configured=true,updated_at=now() WHERE id=%s", (bot_id,))
            if closed:
                self.close(sid)
                conn.execute("UPDATE terminal_sessions SET state=CASE WHEN state='error' THEN state ELSE 'closed' END,updated_at=now() WHERE id=%s", (sid,))

    def cleanup(self, conn):
        conn.execute("DELETE FROM terminal_inputs WHERE state='done' AND created_at<now()-interval '10 minutes'")
        conn.execute("DELETE FROM terminal_outputs WHERE created_at<now()-interval '24 hours'")
        stale = conn.execute("SELECT id FROM terminal_sessions WHERE state IN ('opening','open','closing') AND last_activity_at<now()-interval '2 hours'").fetchall()
        for row in stale:
            self.close(str(row['id']))
        conn.execute("UPDATE terminal_sessions SET state='closed',updated_at=now() WHERE state IN ('opening','open','closing') AND last_activity_at<now()-interval '2 hours'")

    def shutdown(self):
        self.stopping.set()
        with self.lock:
            ids = list(self.sessions)
        for sid in ids:
            self.close(sid)
