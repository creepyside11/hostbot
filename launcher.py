"""Start the stable Emerald worker plus SQLite companion."""
import os
import runpy
import threading
import time

import psycopg
import private_repo_patch  # noqa: F401 - applies Runner patch on import
from db_connection import connection_string, connect, operational_error_summary, validate_worker_database_url
from sqlite_inspector import watch_forever as sqlite_watch_forever


def wait_for_database():
    """Avoid a platform restart loop while PostgreSQL is temporarily unavailable."""
    try:
        validate_worker_database_url()
        os.environ['DATABASE_URL'] = connection_string()
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else 'DATABASE_URL исполнителя имеет некорректный формат.', flush=True)
        raise SystemExit(1)

    last_notice = None
    last_print = 0.0
    while True:
        try:
            with connect() as conn:
                conn.execute('SELECT 1')
            print('PostgreSQL: соединение исполнителя готово.', flush=True)
            return
        except psycopg.OperationalError as error:
            notice = operational_error_summary(error)
            now = time.monotonic()
            if notice != last_notice or now - last_print >= 300:
                print(f'{notice} Повтор через 15 секунд.', flush=True)
                last_notice = notice
                last_print = now
            time.sleep(15)
        except Exception:
            print('PostgreSQL: ошибка конфигурации исполнителя. Проверьте DATABASE_URL.', flush=True)
            raise SystemExit(1)


wait_for_database()
threading.Thread(target=sqlite_watch_forever, name='sqlite-inspector', daemon=True).start()
runpy.run_path('/opt/emerald/main.py', run_name='__main__')
