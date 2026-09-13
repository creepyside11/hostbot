"""Emerald Host manager: PostgreSQL job worker plus a Telegram /start command."""
import concurrent.futures
import json
import os
import queue
import re
import signal
import threading
import time
import urllib.request
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
from runner import Runner, decrypt, redact
from template_runner import prepare_template
from terminal_bridge import TerminalBridge

STOP = threading.Event()
LOGS = queue.Queue(maxsize=2000)


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                          options='-c statement_timeout=15000')


def log(bot_id, message, stream="runtime"):
    try:
        LOGS.put_nowait((bot_id, message[:4000], stream))
    except queue.Full:
        pass


def flush_logs():
    batch = []
    for _ in range(500):
        try:
            batch.append(LOGS.get_nowait())
        except queue.Empty:
            break
    if batch:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO logs(bot_id,message,stream) SELECT id,%s,%s FROM bots WHERE id=%s',
                                [(message, stream, bot_id) for bot_id, message, stream in batch])
            for bot_id, stream in {(item[0],item[2]) for item in batch}:
                conn.execute('DELETE FROM logs WHERE bot_id=%s AND stream=%s AND id NOT IN (SELECT id FROM logs WHERE bot_id=%s AND stream=%s ORDER BY id DESC LIMIT 1000)', (bot_id, stream, bot_id, stream))


def telegram():
    """No management commands or credentials in Telegram; only /start is answered."""
    token = os.environ['BOT_TOKEN']
    base = f'https://api.telegram.org/bot{token}/'
    offset = 0
    def request(method, payload):
        req = urllib.request.Request(base + method, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=40) as response:
            data = json.load(response)
        if not data.get('ok'):
            raise RuntimeError('Telegram API failed')
        return data['result']
    while not STOP.is_set():
        try:
            request('deleteWebhook', {'drop_pending_updates': False})
            break
        except Exception:
            print('Telegram недоступен; повтор через 10 секунд.', flush=True)
            STOP.wait(10)
    last_replies = {}
    while not STOP.is_set():
        try:
            updates = request('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message']})
            for update in updates:
                offset = update['update_id'] + 1
                message = update.get('message', {})
                text = message.get('text', '')
                if re.fullmatch(r'/start(?:@[A-Za-z0-9_]+)?(?:\s+.*)?', text):
                    chat = message['chat']['id']
                    now = time.monotonic()
                    if now - last_replies.get(chat, 0) < 5:
                        continue
                    last_replies[chat] = now
                    if len(last_replies) > 1000:
                        last_replies = {k: v for k, v in last_replies.items() if now-v < 60}
                    request('sendMessage', {'chat_id': chat,
                            'text': 'Emerald Host 💎\nЯ запускаю ваших ботов. Управление, статусы и логи доступны на сайте:\n' + os.environ['SITE_URL'],
                            'link_preview_options': {'is_disabled': True}})
        except Exception:
            print('Ошибка связи Telegram. Повтор через 10 секунд.', flush=True)
            STOP.wait(10)


def execute_job(job, runner):
    bot_id = str(job['bot_id'])
    secret = {}
    try:
        with connect() as conn:
            bot = conn.execute('SELECT * FROM bots WHERE id=%s', (bot_id,)).fetchone()
        if not bot:
            return
        if bot.get('build_mode')=='dockerfile': runner.docker_bots.add(bot_id)
        action = job['action']
        log(bot_id, {'deploy':'Деплой', 'start':'Запуск', 'restart':'Перезапуск', 'update':'Обновление из GitHub',
                     'stop':'Остановка', 'delete':'Удаление'}[action] + ': задание получено.', 'build' if action in ('deploy','update') else 'runtime')
        if action == 'delete':
            runner.delete(bot_id)
            with connect() as conn:
                conn.execute('DELETE FROM bots WHERE id=%s', (bot_id,))
            return
        if action == 'stop':
            runner.stop(bot_id)
            status = 'stopped'
            log(bot_id, 'Бот остановлен.')
        else:
            if bot.get('template_id') and not bot.get('template_configured') and action in ('start','restart'):
                runner.stop(bot_id)
                log(bot_id, 'Сначала завершите первичную настройку шаблона в терминале.')
                with connect() as conn:
                    conn.execute("UPDATE bots SET status='stopped',desired='stopped',updated_at=now() WHERE id=%s", (bot_id,))
                    conn.execute("UPDATE jobs SET state='failed',finished_at=now() WHERE id=%s", (job['id'],))
                return
            secret = decrypt(bot['secrets'], os.environ['ENCRYPTION_KEY'])
            if action == 'deploy' and bot.get('template_id') and bot.get('desired') == 'stopped' and not bot.get('template_configured'):
                prepare_template(runner, bot, secret)
                status = 'stopped'
            else:
                resolved = runner.start(bot, secret, rebuild=action in ('deploy', 'update'))
                if resolved:
                    with connect() as conn:
                        conn.execute('UPDATE bots SET entrypoint=%s WHERE id=%s', (resolved,bot_id))
                status = 'running'
        with connect() as conn:
            if action == 'update':
                conn.execute("UPDATE bots SET status=%s,github_last_sha=COALESCE(github_last_attempt_sha,github_last_sha),github_last_attempt_sha=NULL,updated_at=now() WHERE id=%s", (status, bot_id))
            else:
                conn.execute('UPDATE bots SET status=%s,updated_at=now() WHERE id=%s', (status, bot_id))
            conn.execute("UPDATE jobs SET state='done',finished_at=now() WHERE id=%s", (job['id'],))
    except Exception as error:
        try:
            runner.stop(bot_id)
        except Exception:
            log(bot_id,'Не удалось подтвердить остановку контейнера. Проверьте Docker-сервер.')
        message = str(error) if type(error) in (ValueError, RuntimeError) else f'Ошибка выполнения ({type(error).__name__}). Проверьте исходники, доступность сети и настройки.'
        log(bot_id, redact(message, secret), getattr(error,'log_stream','runtime'))
        with connect() as conn:
            conn.execute("UPDATE bots SET status='error',updated_at=now() WHERE id=%s", (bot_id,))
            conn.execute("UPDATE jobs SET state='failed',finished_at=now() WHERE id=%s", (job['id'],))


def ensure_schema(conn):
    """Bootstrap an empty database without deleting existing tables or records."""
    migration = Path(__file__).resolve().parent / 'sql' / '001_init.sql'
    if not migration.is_file():
        raise RuntimeError('В образе отсутствует sql/001_init.sql. Пересоберите контейнер из main.')
    sql = migration.read_text(encoding='utf-8').strip()
    sql = sql.removeprefix('BEGIN;').removesuffix('COMMIT;')
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(739023)')
        conn.execute(sql, prepare=False)
        conn.execute((migration.parent / '002_features.sql').read_text(encoding='utf-8'), prepare=False)
    print('PostgreSQL: таблицы Emerald Host готовы.', flush=True)


def recover(conn):
    conn.execute("UPDATE jobs SET state='pending' WHERE state='active'")
    conn.execute("UPDATE terminal_sessions SET state='closed',updated_at=now(),error_message='Worker перезапущен. Откройте терминал снова.' WHERE state IN ('opening','open','closing')")
    conn.execute("UPDATE terminal_inputs SET state='done' WHERE state IN ('pending','active')")
    conn.execute("UPDATE bots SET status='deploying',updated_at=now() WHERE desired='running'")
    conn.execute("UPDATE bots SET status='stopped',updated_at=now() WHERE desired='stopped'")
    conn.execute("""INSERT INTO jobs(bot_id,action)
        SELECT b.id,CASE WHEN b.desired='deleted' THEN 'delete' ELSE 'start' END FROM bots b
        WHERE b.desired IN ('running','deleted') AND NOT EXISTS
        (SELECT 1 FROM jobs j WHERE j.bot_id=b.id AND j.state IN ('pending','active'))""")
    conn.execute("""INSERT INTO jobs(bot_id,action)
        SELECT b.id,'deploy' FROM bots b
        WHERE b.template_id IS NOT NULL AND b.template_configured=false AND b.desired='stopped'
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.bot_id=b.id AND j.state IN ('pending','active'))""")


def queue_github_webhook_update(conn):
    """Turn one coalesced GitHub push event into a normal update job without polling GitHub."""
    conn.execute("""DELETE FROM github_webhook_updates q USING bots b
        WHERE q.bot_id=b.id AND (b.auto_update=false OR b.desired!='running')""")
    row = conn.execute("""SELECT q.bot_id,q.sha FROM github_webhook_updates q
        JOIN bots b ON b.id=q.bot_id
        WHERE b.auto_update=true AND b.desired='running'
          AND b.github_last_sha IS DISTINCT FROM q.sha
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.bot_id=b.id AND j.state IN ('pending','active'))
        ORDER BY q.received_at
        FOR UPDATE OF q SKIP LOCKED LIMIT 1""").fetchone()
    if not row:
        return False
    conn.execute("UPDATE bots SET github_last_attempt_sha=%s,github_last_check=now(),status='deploying',updated_at=now() WHERE id=%s",
                 (row['sha'], row['bot_id']))
    conn.execute("INSERT INTO jobs(bot_id,action) VALUES(%s,'update')", (row['bot_id'],))
    conn.execute('DELETE FROM github_webhook_updates WHERE bot_id=%s', (row['bot_id'],))
    conn.execute("INSERT INTO logs(bot_id,message,stream) VALUES(%s,%s,'build')",
                 (row['bot_id'], f"GitHub webhook: push {row['sha'][:8]}, обновление поставлено в очередь."))
    return True


def check_storage(runner):
    limit = int(os.getenv('MAX_WORKDIR_MB', '1024')) * 1024 * 1024
    for home in runner.root.iterdir():
        if not home.is_dir():
            continue
        total = 0
        for parent, dirs, files in os.walk(home, followlinks=False):
            for filename in files:
                try:
                    total += (Path(parent) / filename).lstat().st_size
                except OSError:
                    pass
            if total > limit:
                bot_id = home.name
                runner.stop(bot_id)
                with runner.lock:
                    build = runner.builds.get(bot_id)
                if build:
                    from runner import kill_group
                    kill_group(build)
                log(bot_id, 'Превышен лимит размера проекта. Удалите бота и уменьшите проект или увеличьте MAX_WORKDIR_MB.')
                with connect() as conn:
                    conn.execute("UPDATE bots SET status='error',desired='stopped',updated_at=now() WHERE id=%s AND status!='error'", (bot_id,))
                break


def main():
    for name in ('DATABASE_URL','ENCRYPTION_KEY','BOT_TOKEN','SITE_URL'):
        if not os.getenv(name):
            raise SystemExit(f'Задайте переменную {name} в Bothost.')
    if not re.fullmatch('[a-fA-F0-9]{64}', os.environ['ENCRYPTION_KEY']):
        raise SystemExit('ENCRYPTION_KEY: требуются 64 hex-символа, одинаковые с сайтом.')
    if os.getenv('TRUSTED_CODE_ONLY') != 'true':
        raise SystemExit('Этот исполнитель только для доверенного кода. Установите TRUSTED_CODE_ONLY=true.')
    if not re.fullmatch(r'\d{5,}:[A-Za-z0-9_-]{20,}', os.environ['BOT_TOKEN']):
        raise SystemExit('Некорректный BOT_TOKEN управляющего бота.')
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    runner = Runner(os.getenv('DATA_DIR', '/app/data/emerald'), log)
    terminal = TerminalBridge(runner)
    guard = connect()
    guard.autocommit = True
    if not guard.execute('SELECT pg_try_advisory_lock(739022) AS locked').fetchone()['locked']:
        guard.close()
        raise SystemExit('Другой исполнитель уже подключён. Используйте одну реплику.')
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    active_bot_id = None
    tick = 0
    try:
        ensure_schema(guard)
        if runner.docker:
            for row in guard.execute("SELECT id FROM bots WHERE build_mode='dockerfile'").fetchall():
                runner.docker.stop_existing(str(row['id']))
        with guard.transaction():
            recover(guard)
        threading.Thread(target=telegram, daemon=True).start()
        print('Emerald Host: исполнитель запущен, GitHub auto-update и Docker PTY активны.', flush=True)
        while not STOP.is_set():
            guard.execute("INSERT INTO worker_health(id,heartbeat,version,supports_docker,supports_terminal) VALUES('nl',now(),'1.3.0',%s,%s) ON CONFLICT(id) DO UPDATE SET heartbeat=now(),version='1.3.0',supports_docker=EXCLUDED.supports_docker,supports_terminal=EXCLUDED.supports_terminal", (runner.docker is not None, runner.docker is not None))
            flush_logs()
            terminal.tick(guard)
            for bot_id, code in runner.exited(exclude=active_bot_id if future and not future.done() else None):
                log(bot_id, f'Процесс завершился (код {code}). Для повторного запуска используйте панель.')
                guard.execute("UPDATE bots SET status='error',updated_at=now() WHERE id=%s", (bot_id,))
            if future is None or future.done():
                if future:
                    future.result()
                with guard.transaction():
                    queue_github_webhook_update(guard)
                    job = guard.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
                    if job:
                        guard.execute("UPDATE jobs SET state='active' WHERE id=%s", (job['id'],))
                active_bot_id = str(job["bot_id"]) if job else None
                future = executor.submit(execute_job, job, runner) if job else None
            if tick % 30 == 0:
                check_storage(runner)
                terminal.cleanup(guard)
                guard.execute('DELETE FROM sessions WHERE expires_at<now()')
                guard.execute('DELETE FROM rate_limits WHERE expires_at<now()')
                guard.execute("DELETE FROM jobs WHERE state IN ('done','failed') AND finished_at<now()-interval '7 days'")
            tick += 1
            STOP.wait(2)
    finally:
        STOP.set()
        terminal.shutdown()
        runner.shutdown()
        executor.shutdown(wait=True, cancel_futures=True)
        try:
            flush_logs()
            guard.execute("UPDATE worker_health SET heartbeat=now()-interval '1 hour' WHERE id='nl'")
        except Exception:
            pass
        guard.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if isinstance(error, psycopg.errors.InsufficientPrivilege):
            print('Нет прав PostgreSQL: пользователю DATABASE_URL нужны CREATE в схеме и доступ к таблицам Emerald Host. Выдайте права или выполните sql/001_init.sql владельцем БД.', flush=True)
        elif isinstance(error, psycopg.errors.UndefinedTable):
            print('Не найдена таблица PostgreSQL. Проверьте, что сайт и исполнитель используют одну БД и схему; обновите контейнер из main.', flush=True)
        elif isinstance(error, RuntimeError):
            print(str(error), flush=True)
        else:
            print(f'Исполнитель остановлен ({type(error).__name__}). Проверьте БД и окружение.', flush=True)
        raise SystemExit(1)
