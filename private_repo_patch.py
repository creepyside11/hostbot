"""Keep GitHub OAuth credentials out of user-bot env while letting Runner download private sources."""
import os
import psycopg
import runner
from github_worker import github_token, download_archive

_original_prepare = runner.Runner.prepare


def _prepare(self, bot, secrets):
    if bot.get('source') == 'github':
        encrypted = None
        try:
            with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=8) as conn:
                row = conn.execute('SELECT github_token FROM users WHERE id=%s', (bot['user_id'],)).fetchone()
                encrypted = row[0] if row else None
        except Exception:
            encrypted = None
        if encrypted:
            try:
                token = github_token(encrypted, os.environ['ENCRYPTION_KEY'])
                if token:
                    self.log(str(bot['id']), 'GitHub OAuth: получение исходников через подключённый аккаунт…', 'build')
                    raw = download_archive(bot['repo'], bot['branch'], token, self.closing)
                    patched = dict(bot)
                    patched['source'] = 'zip'
                    patched['archive'] = raw
                    return _original_prepare(self, patched, secrets)
            except (ValueError, RuntimeError):
                raise
            except Exception:
                raise RuntimeError('Не удалось получить private GitHub-репозиторий. Переподключите GitHub в профиле.') from None
    return _original_prepare(self, bot, secrets)


runner.Runner.prepare = _prepare
