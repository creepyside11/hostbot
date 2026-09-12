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

STOP = threading.Event()
LOGS = queue.Queue(maxsize=2000)


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                          options='-c statement_timeout=15000')


def log(bot_id, message):
    try:
        LOGS.put_nowait((bot_id, message[:4000]))
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
            # Delete may have removed the bot before a reader finished draining stdout.
            with conn.cursor() as cur:
                cur.executemany('INSERT INTO logs(bot_id,message) SELECT id,%s FROM bots WHERE id=%s',
                                [(message, bot_id) for bot_id, message in batch])
            for bot_id in {item[0] for item in batch}:
                conn.execute('DELETE FROM logs WHERE bot_id=%s AND id NOT IN (SELECT id FROM logs WHERE bot_id=%s ORDER BY id DESC LIMIT 1000)', (bot_id, bot_id))


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
    # Long polling requires no inbound port on Bothost.
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
            # Never print exception URLs: Telegram embeds BOT_TOKEN in the URL.
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
        action = job['action']
        log(bot_id, {'deploy':'Деплой', 'start':'Запуск', 'restart':'Перезапуск', 'update':'Обновление из GitHub',
                     'stop':'Остановка', 'delete':'Удаление'}[action] + ': задание получено.')
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
            secret = decrypt(bot['secrets'], os.environ['ENCRYPTION_KEY'])
            runner.start(bot, secret, rebuild=action in ('deploy', 'update'))
            status = 'running'
        with connect() as conn:
            conn.execute('UPDATE bots SET status=%s,updated_at=now() WHERE id=%s', (status, bot_id))
            conn.execute("UPDATE jobs SET state='done',finished_at=now() WHERE id=%s", (job['id'],))
    except Exception as error:
        runner.stop(bot_id)
        # Curated validation/runtime errors are actionable; library errors may contain credentials.
        message = str(error) if type(error) in (ValueError, RuntimeError) else f'Ошибка выполнения ({type(error).__name__}). Проверьте исходники, доступность сети и настройки.'
        log(bot_id, redact(message, secret))
        with connect() as conn:
            conn.execute("UPDATE bots SET status='error',updated_at=now() WHERE id=%s", (bot_id,))
            conn.execute("UPDATE jobs SET state='failed',finished_at=now() WHERE id=%s", (job['id'],))


def recover(conn):
    # A container restart kills its child processes. Re-run interrupted commands, then restore desired bots.
    conn.execute("UPDATE jobs SET state='pending' WHERE state='active'")
    conn.execute("UPDATE bots SET status='deploying',updated_at=now() WHERE desired='running'")
    conn.execute("UPDATE bots SET status='stopped',updated_at=now() WHERE desired='stopped'")
    conn.execute("""INSERT INTO jobs(bot_id,action)
        SELECT b.id,CASE WHEN b.desired='deleted' THEN 'delete' ELSE 'start' END FROM bots b
        WHERE b.desired IN ('running','deleted') AND NOT EXISTS
        (SELECT 1 FROM jobs j WHERE j.bot_id=b.id AND j.state IN ('pending','active'))""")


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
                # Applying a hard disk quota requires platform support. Stop runtime and retain files for inspection.
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
    # Must use a DIRECT or session-mode PostgreSQL URL, not a transaction pooler.
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
        with guard.transaction():
            recover(guard)
        threading.Thread(target=telegram, daemon=True).start()
        print('Emerald Host: исполнитель запущен, ожидаю задания.', flush=True)
        while not STOP.is_set():
            # Loss of the lock-owning connection terminates all children to avoid duplicate polling.
            guard.execute("INSERT INTO worker_health(id,heartbeat,version) VALUES('nl',now(),'1.0.0') ON CONFLICT(id) DO UPDATE SET heartbeat=now(),version='1.0.0'")
            flush_logs()
            for bot_id, code in runner.exited(exclude=active_bot_id if future and not future.done() else None):
                log(bot_id, f'Процесс завершился (код {code}). Для повторного запуска используйте панель.')
                guard.execute("UPDATE bots SET status='error',updated_at=now() WHERE id=%s", (bot_id,))
            if future is None or future.done():
                if future:
                    future.result()
                with guard.transaction():
                    job = guard.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
                    if job:
                        guard.execute("UPDATE jobs SET state='active' WHERE id=%s", (job['id'],))
                active_bot_id = str(job["bot_id"]) if job else None
                future = executor.submit(execute_job, job, runner) if job else None
            if tick % 30 == 0:
                check_storage(runner)
                guard.execute('DELETE FROM sessions WHERE expires_at<now()')
                guard.execute('DELETE FROM rate_limits WHERE expires_at<now()')
                guard.execute("DELETE FROM jobs WHERE state IN ('done','failed') AND finished_at<now()-interval '7 days'")
            tick += 1
            STOP.wait(2)
    finally:
        STOP.set()
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
        print(f'Исполнитель остановлен ({type(error).__name__}). Проверьте БД, миграцию и окружение.', flush=True)
        raise SystemExit(1)
