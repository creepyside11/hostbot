"""Poll connected GitHub branches every 30 seconds and enqueue update jobs when SHA changes."""
import os
import time
import psycopg
from psycopg.rows import dict_row
from github_worker import github_token, branch_sha

INTERVAL = 30


def connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                          options='-c statement_timeout=15000')


def check_once():
    with connect() as conn:
        rows = conn.execute("""SELECT b.id,b.repo,b.branch,b.github_last_sha,b.github_last_attempt_sha,u.github_token
            FROM bots b JOIN users u ON u.id=b.user_id
            WHERE b.source='github' AND b.auto_update=true AND b.desired='running'
            ORDER BY COALESCE(b.github_last_check,to_timestamp(0)) ASC,b.created_at ASC""").fetchall()
    for row in rows:
        if not row.get('github_token'):
            with connect() as conn:
                conn.execute('UPDATE bots SET auto_update=false WHERE id=%s', (row['id'],))
            continue
        try:
            token = github_token(row['github_token'], os.environ['ENCRYPTION_KEY'])
            current_sha = branch_sha(row['repo'], row['branch'], token)
        except Exception:
            with connect() as conn:
                conn.execute('UPDATE bots SET github_last_check=now() WHERE id=%s', (row['id'],))
            continue
        with connect() as conn:
            with conn.transaction():
                bot = conn.execute('SELECT id,status,github_last_sha,github_last_attempt_sha,auto_update,desired FROM bots WHERE id=%s FOR UPDATE', (row['id'],)).fetchone()
                if not bot or not bot['auto_update'] or bot['desired'] != 'running':
                    continue
                conn.execute('UPDATE bots SET github_last_check=now() WHERE id=%s', (row['id'],))
                busy = conn.execute("SELECT 1 FROM jobs WHERE bot_id=%s AND state IN ('pending','active')", (row['id'],)).fetchone()
                if not bot['github_last_sha']:
                    conn.execute('UPDATE bots SET github_last_sha=%s,github_last_attempt_sha=NULL WHERE id=%s', (current_sha,row['id']))
                    continue
                if current_sha == bot['github_last_sha']:
                    if bot['github_last_attempt_sha']:
                        conn.execute('UPDATE bots SET github_last_attempt_sha=NULL WHERE id=%s', (row['id'],))
                    continue
                if current_sha == bot['github_last_attempt_sha']:
                    if not busy and bot['status'] == 'running':
                        conn.execute('UPDATE bots SET github_last_sha=%s,github_last_attempt_sha=NULL WHERE id=%s', (current_sha,row['id']))
                    # Failed deploy keeps github_last_attempt_sha, so this exact commit is not retried forever.
                    continue
                if busy:
                    continue
                conn.execute("UPDATE bots SET github_last_attempt_sha=%s,status='deploying',updated_at=now() WHERE id=%s", (current_sha,row['id']))
                conn.execute("INSERT INTO jobs(bot_id,action) VALUES(%s,'update')", (row['id'],))
                conn.execute("INSERT INTO logs(bot_id,message,stream) VALUES(%s,%s,'build')", (row['id'],f'GitHub auto-update: найден новый commit {current_sha[:8]}, обновление поставлено в очередь.'))


def watch_forever():
    # main.py performs the schema migration. Retry quietly until it is ready.
    while True:
        try:
            check_once()
            time.sleep(INTERVAL)
        except (psycopg.errors.UndefinedColumn, psycopg.errors.UndefinedTable):
            time.sleep(5)
        except Exception:
            # Do not print exception details: database URLs or provider errors may contain credentials.
            print('GitHub auto-update: временная ошибка проверки, повтор через 30 секунд.', flush=True)
            time.sleep(INTERVAL)
