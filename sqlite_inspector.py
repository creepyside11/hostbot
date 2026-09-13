"""Read-only SQLite explorer for Emerald Host dashboard and Public API."""
import base64
import os
import sqlite3
import time
import urllib.parse
from contextlib import closing
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(os.getenv('DATA_DIR', '/app/data/emerald')).resolve()
SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'tmp'}
SQLITE_EXTENSIONS = {'.db', '.sqlite', '.sqlite3'}


def pg_connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                           options='-c statement_timeout=15000')


def source_root(bot_id):
    source = (ROOT / str(bot_id) / 'source').resolve()
    expected = (ROOT / str(bot_id)).resolve()
    if not source.is_relative_to(expected) or not source.is_dir():
        raise RuntimeError('Файлы проекта ещё не готовы на worker.')
    return source


def resolve_database(bot_id, relative):
    source = source_root(bot_id)
    value = str(relative or '').replace('\\', '/').strip()
    if not value or value.startswith('/') or '..' in Path(value).parts:
        raise ValueError('Некорректный путь SQLite-файла.')
    target = (source / value).resolve()
    if not target.is_relative_to(source) or not target.is_file():
        raise ValueError('SQLite-файл не найден.')
    with target.open('rb') as handle:
        if handle.read(16) != b'SQLite format 3\x00':
            raise ValueError('Выбранный файл не является SQLite 3 database.')
    return target


def discover_files(bot_id):
    source = source_root(bot_id)
    found = []
    scanned = 0
    for parent, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
        base = Path(parent)
        for name in files:
            scanned += 1
            if scanned > 12000 or len(found) >= 80:
                break
            path = base / name
            if path.suffix.lower() not in SQLITE_EXTENSIONS:
                continue
            try:
                if path.is_symlink() or path.stat().st_size < 16:
                    continue
                with path.open('rb') as handle:
                    if handle.read(16) != b'SQLite format 3\x00':
                        continue
                stat = path.stat()
                found.append({'path': path.relative_to(source).as_posix(), 'size': stat.st_size,
                              'modified_at': stat.st_mtime})
            except OSError:
                continue
        if scanned > 12000 or len(found) >= 80:
            break
    found.sort(key=lambda item: item['path'].lower())
    return {'files': found, 'truncated': len(found) >= 80 or scanned > 12000}


def open_readonly(path):
    uri = 'file:' + urllib.parse.quote(str(path)) + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=1.5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=1500')
    deadline = time.monotonic() + 4
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    return conn


def qident(value):
    return '"' + str(value).replace('"', '""') + '"'


def scalar(value):
    if value is None or isinstance(value, (int, float, str)):
        if isinstance(value, str) and len(value) > 20000:
            return value[:20000] + '…'
        return value
    if isinstance(value, bytes):
        preview = value[:3072]
        return {'type': 'blob', 'bytes': len(value), 'base64_preview': base64.b64encode(preview).decode(),
                'truncated': len(value) > len(preview)}
    return str(value)[:20000]


def database_tables(path):
    with closing(open_readonly(path)) as conn:
        rows = conn.execute("""SELECT type,name,tbl_name,sql FROM sqlite_master
            WHERE type IN ('table','view') ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name COLLATE NOCASE""").fetchall()
        return {'tables': [{'type': row['type'], 'name': row['name'], 'table': row['tbl_name'],
                            'sql': row['sql'], 'system': str(row['name']).startswith('sqlite_')} for row in rows]}


def database_schema(path):
    with closing(open_readonly(path)) as conn:
        rows = conn.execute("""SELECT type,name,tbl_name,sql FROM sqlite_master
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 WHEN 'index' THEN 2 ELSE 3 END,name COLLATE NOCASE""").fetchall()
        return {'schema': [{'type': row['type'], 'name': row['name'], 'table': row['tbl_name'], 'sql': row['sql']} for row in rows]}


def database_rows(path, table, offset, limit):
    offset = max(0, min(int(offset or 0), 10_000_000))
    limit = max(1, min(int(limit or 100), 250))
    quoted = qident(table)
    with closing(open_readonly(path)) as conn:
        exists = conn.execute("SELECT type FROM sqlite_master WHERE name=? AND type IN ('table','view')", (table,)).fetchone()
        if not exists:
            raise ValueError('Таблица или view не найдены.')
        columns = [item[1] for item in conn.execute(f'PRAGMA table_info({quoted})').fetchall()]
        if not columns:
            cursor = conn.execute(f'SELECT * FROM {quoted} LIMIT 0')
            columns = [d[0] for d in (cursor.description or [])]
        total = conn.execute(f'SELECT count(*) FROM {quoted}').fetchone()[0]
        rows = conn.execute(f'SELECT * FROM {quoted} LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        data = [[scalar(row[column]) for column in columns] for row in rows]
        return {'table': table, 'columns': columns, 'rows': data, 'total': int(total), 'offset': offset,
                'limit': limit, 'has_more': offset + len(data) < int(total)}


def execute_request(request):
    operation = request['operation']
    if operation == 'files':
        return discover_files(request['bot_id'])
    path = resolve_database(request['bot_id'], request['file_path'])
    base = {'file': request['file_path'], 'size': path.stat().st_size}
    if operation == 'tables':
        return {**base, **database_tables(path)}
    if operation == 'schema':
        return {**base, **database_schema(path)}
    if operation == 'rows':
        return {**base, **database_rows(path, request['table_name'], request['row_offset'], request['row_limit'])}
    raise ValueError('Неизвестная SQLite-операция.')


def claim_one():
    with pg_connect() as conn:
        with conn.transaction():
            conn.execute("UPDATE sqlite_requests SET state='failed',error='SQLite request expired',finished_at=now() WHERE state IN ('pending','active') AND expires_at<now()")
            row = conn.execute("SELECT * FROM sqlite_requests WHERE state='pending' AND expires_at>now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE sqlite_requests SET state='active',started_at=now() WHERE id=%s", (row['id'],))
            return row


def finish(request_id, result=None, error=None):
    with pg_connect() as conn:
        if error:
            conn.execute("UPDATE sqlite_requests SET state='failed',error=%s,finished_at=now() WHERE id=%s", (str(error)[:500], request_id))
        else:
            conn.execute("UPDATE sqlite_requests SET state='done',result=%s,finished_at=now() WHERE id=%s", (Jsonb(result), request_id))
        conn.execute("DELETE FROM sqlite_requests WHERE finished_at<now()-interval '1 day'")


def watch_forever():
    while True:
        try:
            request = claim_one()
            if not request:
                time.sleep(0.5)
                continue
            try:
                finish(request['id'], result=execute_request(request))
            except (ValueError, RuntimeError, sqlite3.Error, OSError) as error:
                finish(request['id'], error=error)
            except Exception:
                finish(request['id'], error='SQLite inspector failed safely. Проверьте файл и повторите.')
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            time.sleep(3)
        except Exception:
            print('SQLite inspector: временная ошибка, повтор через 3 секунды.', flush=True)
            time.sleep(3)
