"""Safe project file manager companion for Emerald Host dashboard."""
import base64
import os
import shutil
import time
import uuid
from pathlib import Path, PurePosixPath

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(os.getenv('DATA_DIR', '/app/data/emerald')).resolve()
BLOCKED_PARTS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', 'tmp'}
MAX_TEXT_BYTES = 512 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 500


def pg_connect():
    return psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10, row_factory=dict_row,
                           options='-c statement_timeout=15000')


def ensure_schema():
    with pg_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS file_requests(
            id uuid PRIMARY KEY,
            bot_id uuid NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            operation text NOT NULL CHECK(operation IN ('list','read','write','mkdir','rename','delete','upload')),
            file_path text,
            target_path text,
            payload text,
            state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','done','failed')),
            result jsonb,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            expires_at timestamptz NOT NULL DEFAULT now()+interval '2 minutes'
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS file_requests_queue ON file_requests(state,created_at) WHERE state='pending'")
        conn.execute("CREATE INDEX IF NOT EXISTS file_requests_owner ON file_requests(user_id,created_at DESC)")


def source_root(bot_id):
    home = (ROOT / str(bot_id)).resolve()
    source = (home / 'source').resolve()
    if not source.is_relative_to(home) or not source.is_dir():
        raise RuntimeError('Файлы проекта ещё не готовы на worker.')
    return source


def clean_relative(value, allow_empty=False):
    raw = str(value or '').replace('\\', '/').strip('/')
    if not raw:
        if allow_empty:
            return Path()
        raise ValueError('Укажите путь файла или папки.')
    if len(raw) > 320 or '\x00' in raw:
        raise ValueError('Некорректный путь проекта.')
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in ('', '.', '..') for part in pure.parts):
        raise ValueError('Некорректный путь проекта.')
    for part in pure.parts:
        low = part.lower()
        if part in BLOCKED_PARTS or low == '.env' or low.startswith('.env.'):
            raise ValueError('Этот служебный путь недоступен через файловый менеджер.')
        if any(ord(ch) < 32 for ch in part):
            raise ValueError('Некорректный путь проекта.')
    return Path(*pure.parts)


def resolve_target(bot_id, value, allow_empty=False, must_exist=False):
    source = source_root(bot_id)
    relative = clean_relative(value, allow_empty=allow_empty)
    target = (source / relative).resolve(strict=False)
    if not target.is_relative_to(source):
        raise ValueError('Путь выходит за пределы проекта.')
    current = source
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError('Символьные ссылки недоступны через файловый менеджер.')
    if must_exist and not target.exists():
        raise ValueError('Файл или папка не найдены.')
    return source, target, relative


def atomic_write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise ValueError('Нельзя записывать через символьную ссылку.')
    old_mode = target.stat().st_mode & 0o777 if target.exists() and target.is_file() else None
    temp = target.with_name(f'.{target.name}.emerald-{uuid.uuid4().hex}.tmp')
    try:
        temp.write_bytes(data)
        if old_mode is not None:
            os.chmod(temp, old_mode)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def list_directory(bot_id, relative):
    source, directory, rel = resolve_target(bot_id, relative, allow_empty=True, must_exist=True)
    if not directory.is_dir():
        raise ValueError('Выбранный путь не является папкой.')
    rows = []
    for item in directory.iterdir():
        name = item.name
        low = name.lower()
        if name in BLOCKED_PARTS or low == '.env' or low.startswith('.env.') or item.is_symlink():
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        is_dir = item.is_dir()
        rows.append({
            'name': name,
            'path': item.relative_to(source).as_posix(),
            'type': 'directory' if is_dir else 'file',
            'size': 0 if is_dir else stat.st_size,
            'modified_at': stat.st_mtime,
            'editable': (not is_dir and stat.st_size <= MAX_TEXT_BYTES),
        })
        if len(rows) >= MAX_ENTRIES:
            break
    rows.sort(key=lambda row: (row['type'] != 'directory', row['name'].lower()))
    parent = rel.parent.as_posix() if rel.parts else ''
    if parent == '.':
        parent = ''
    return {'path': rel.as_posix() if rel.parts else '', 'parent': parent, 'entries': rows,
            'truncated': len(rows) >= MAX_ENTRIES}


def read_text(bot_id, relative):
    _, target, rel = resolve_target(bot_id, relative, must_exist=True)
    if not target.is_file():
        raise ValueError('Выбранный путь не является файлом.')
    size = target.stat().st_size
    if size > MAX_TEXT_BYTES:
        raise ValueError('Редактор поддерживает текстовые файлы до 512 КБ.')
    raw = target.read_bytes()
    if b'\x00' in raw:
        raise ValueError('Бинарный файл нельзя открыть в текстовом редакторе.')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Файл не является UTF-8 текстом.') from None
    return {'path': rel.as_posix(), 'content': text, 'size': len(raw), 'modified_at': target.stat().st_mtime}


def write_text(bot_id, relative, payload):
    _, target, rel = resolve_target(bot_id, relative)
    if target.exists() and not target.is_file():
        raise ValueError('На этом пути уже существует папка.')
    data = str(payload or '').encode('utf-8')
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError('Редактор поддерживает текстовые файлы до 512 КБ.')
    atomic_write(target, data)
    return {'path': rel.as_posix(), 'size': len(data), 'saved': True}


def make_directory(bot_id, relative):
    _, target, rel = resolve_target(bot_id, relative)
    if target.exists():
        raise ValueError('Такой файл или папка уже существует.')
    target.mkdir(parents=False)
    return {'path': rel.as_posix(), 'created': True}


def rename_entry(bot_id, relative, target_relative):
    source, current, _ = resolve_target(bot_id, relative, must_exist=True)
    _, target, rel = resolve_target(bot_id, target_relative)
    if current == source:
        raise ValueError('Корень проекта переименовать нельзя.')
    if target.exists():
        raise ValueError('На новом пути уже существует файл или папка.')
    if target.parent != current.parent:
        raise ValueError('Переименование доступно только внутри текущей папки.')
    current.rename(target)
    return {'path': rel.as_posix(), 'renamed': True}


def delete_entry(bot_id, relative):
    source, target, rel = resolve_target(bot_id, relative, must_exist=True)
    if target == source:
        raise ValueError('Корень проекта удалить нельзя.')
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {'path': rel.as_posix(), 'deleted': True}


def upload_file(bot_id, relative, payload):
    _, target, rel = resolve_target(bot_id, relative)
    if target.exists() and target.is_dir():
        raise ValueError('На этом пути уже существует папка.')
    try:
        data = base64.b64decode(str(payload or ''), validate=True)
    except Exception:
        raise ValueError('Некорректные данные загружаемого файла.') from None
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError('Максимальный размер одного файла — 2 МБ.')
    atomic_write(target, data)
    return {'path': rel.as_posix(), 'size': len(data), 'uploaded': True}


def execute_request(request):
    operation = request['operation']
    bot_id = request['bot_id']
    if operation == 'list':
        return list_directory(bot_id, request.get('file_path') or '')
    if operation == 'read':
        return read_text(bot_id, request.get('file_path'))
    if operation == 'write':
        return write_text(bot_id, request.get('file_path'), request.get('payload'))
    if operation == 'mkdir':
        return make_directory(bot_id, request.get('file_path'))
    if operation == 'rename':
        return rename_entry(bot_id, request.get('file_path'), request.get('target_path'))
    if operation == 'delete':
        return delete_entry(bot_id, request.get('file_path'))
    if operation == 'upload':
        return upload_file(bot_id, request.get('file_path'), request.get('payload'))
    raise ValueError('Неизвестная файловая операция.')


def claim_one():
    with pg_connect() as conn:
        with conn.transaction():
            conn.execute("UPDATE file_requests SET state='failed',error='File request expired',payload=NULL,finished_at=now() WHERE state IN ('pending','active') AND expires_at<now()")
            row = conn.execute("SELECT * FROM file_requests WHERE state='pending' AND expires_at>now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
            if not row:
                return None
            conn.execute("UPDATE file_requests SET state='active',started_at=now(),payload=NULL WHERE id=%s", (row['id'],))
            return row


def finish(request_id, result=None, error=None):
    with pg_connect() as conn:
        if error:
            conn.execute("UPDATE file_requests SET state='failed',error=%s,payload=NULL,finished_at=now() WHERE id=%s", (str(error)[:500], request_id))
        else:
            conn.execute("UPDATE file_requests SET state='done',result=%s,payload=NULL,finished_at=now() WHERE id=%s", (Jsonb(result), request_id))
        conn.execute("DELETE FROM file_requests WHERE finished_at<now()-interval '1 hour'")


def watch_forever():
    while True:
        try:
            ensure_schema()
            break
        except Exception:
            print('File manager: схема временно недоступна, повтор через 3 секунды.', flush=True)
            time.sleep(3)
    while True:
        try:
            request = claim_one()
            if not request:
                time.sleep(0.35)
                continue
            try:
                finish(request['id'], result=execute_request(request))
            except (ValueError, RuntimeError, OSError) as error:
                finish(request['id'], error=error)
            except Exception:
                finish(request['id'], error='Файловая операция завершилась ошибкой безопасно. Повторите запрос.')
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
            time.sleep(2)
        except Exception:
            print('File manager: временная ошибка, повтор через 3 секунды.', flush=True)
            time.sleep(3)
