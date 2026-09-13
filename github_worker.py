"""GitHub source helpers for private repositories and branch-head checks."""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from runner import decrypt

API = 'https://api.github.com'


def github_token(encrypted, encryption_key):
    if not encrypted:
        return None
    data = decrypt(encrypted, encryption_key)
    token = data.get('access_token') if isinstance(data, dict) else None
    return str(token).strip() if token else None


def repo_slug(repo):
    value = str(repo or '').strip().removesuffix('.git')
    if value.startswith('https://github.com/'):
        value = value.removeprefix('https://github.com/')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise ValueError('Недопустимый GitHub-репозиторий.')
    return value


def github_request(path, token=None, timeout=12):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'EmeraldHost/1.0',
               'X-GitHub-Api-Version': '2022-11-28'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    return urllib.request.Request(API + path, headers=headers)


def branch_sha(repo, branch, token=None):
    slug = repo_slug(repo)
    ref = urllib.parse.quote(str(branch or 'main'), safe='')
    request = github_request(f'/repos/{slug}/branches/{ref}', token)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise RuntimeError('GitHub OAuth больше не даёт доступ к репозиторию. Переподключите GitHub в профиле.') from None
        if error.code == 404:
            raise RuntimeError('GitHub-репозиторий или ветка больше недоступны.') from None
        raise RuntimeError(f'GitHub API временно недоступен ({error.code}).') from None
    sha = data.get('commit', {}).get('sha')
    if not sha:
        raise RuntimeError('GitHub не вернул SHA ветки.')
    return str(sha)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_archive(repo, branch, token, stop_event=None, max_bytes=20 * 1024 * 1024):
    """Download a private repo through the authenticated archive endpoint without forwarding OAuth to codeload."""
    if not token:
        return None
    slug = repo_slug(repo)
    ref = urllib.parse.quote(str(branch or 'main'), safe='')
    request = github_request(f'/repos/{slug}/zipball/{ref}', token)
    opener = urllib.request.build_opener(_NoRedirect)
    location = None
    try:
        opener.open(request, timeout=15)
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308):
            location = error.headers.get('Location')
        elif error.code in (401, 403):
            raise RuntimeError('Нет доступа к private GitHub-репозиторию. Переподключите GitHub в профиле.') from None
        elif error.code == 404:
            raise RuntimeError('GitHub-репозиторий или ветка не найдены.') from None
        else:
            raise RuntimeError(f'Не удалось получить GitHub-архив ({error.code}).') from None
    if not location:
        raise RuntimeError('GitHub не вернул ссылку на архив репозитория.')
    signed = urllib.request.Request(location, headers={'User-Agent': 'EmeraldHost/1.0'})
    deadline = time.monotonic() + 90
    try:
        with urllib.request.urlopen(signed, timeout=20) as response:
            chunks, length = [], 0
            while chunk := response.read(65536):
                length += len(chunk)
                if length > max_bytes:
                    raise ValueError('GitHub-архив больше 20 МБ.')
                if time.monotonic() > deadline or (stop_event and stop_event.is_set()):
                    raise ValueError('Получение кода прервано по таймауту.')
                chunks.append(chunk)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Не удалось скачать GitHub-архив ({error.code}).') from None
    return b''.join(chunks)
