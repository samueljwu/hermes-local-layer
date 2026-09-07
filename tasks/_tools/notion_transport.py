"""Pinned Notion transport and private durable state. Import performs no I/O."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import urllib.error
import urllib.request

SOURCE_ID = '3d463936-8ded-8009-9834-000be85b5473'
DB_ID = '3d463936-8ded-80a7-a2c5-c547ecbb8e8d'
API_VERSION = '2026-03-11'
STATE_ROOT = Path('/home/hermes/.config/hermes-tasks-notion')
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
QUERY_PATH = '/data_sources/' + SOURCE_ID + '/query'

class SyncError(RuntimeError):
    """Fixed diagnostics only: never include remote bodies or task text."""

class SyncBusy(SyncError):
    """Another connector owns the single-writer lock; retry, not a conflict."""

def require(ok, message):
    if not ok:
        raise SyncError(message)

def open_dir(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'unsafe-path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

def private_file(fd):
    s = os.fstat(fd)
    require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and
            stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1, 'unsafe-private-file')

def strict_json(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            require(k not in out, 'duplicate-json-key')
            out[k] = v
        return out
    def bad_constant(_):
        raise SyncError('invalid-json-number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)
    except (ValueError, UnicodeError):
        raise SyncError('invalid-json') from None

class State:
    """Existing private directory; pinned dirfd; nonblocking single-run flock.

    Atomic replace + file and directory fsync precede every external side effect.
    Never creates the config directory or reads credentials on import.
    """
    def __init__(self, root=STATE_ROOT):
        self.root = Path(root)
        self.fd = self.lock = None
    def __enter__(self):
        self.fd = open_dir(self.root)
        try:
            s = os.fstat(self.fd)
            require(s.st_uid == os.getuid() and stat.S_IMODE(s.st_mode) == 0o700, 'unsafe-state-directory')
            # Shared with the previous pilot: do not run pilot and connector together.
            self.lock = os.open('outbound.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd)
            private_file(self.lock)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SyncBusy('sync-busy') from None
            return self
        except BaseException:
            self.__exit__()
            raise
    def __exit__(self, *args):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
    def read_bytes(self, name, *, missing=False):
        require(bool(re.fullmatch(r'[a-z0-9_.-]+', name)) and name not in {'.', '..'}, 'unsafe-filename')
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            if missing:
                return None
            raise
        with os.fdopen(fd, 'rb') as f:
            private_file(f.fileno())
            data = f.read(16 * 1024 * 1024 + 1)
        require(len(data) <= 16 * 1024 * 1024, 'file-size-cap')
        return data
    def read(self, name='two-way.json'):
        data = self.read_bytes(name, missing=True)
        return None if data is None else strict_json(data)
    def write(self, value, name='two-way.json'):
        self.read(name)  # Refuse unsafe existing targets, including corrupt JSON.
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        require(len(raw) <= 16 * 1024 * 1024, 'state-size-cap')
        tmp = 'tmp-' + secrets.token_hex(16) + '.json'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class Client:
    """Public request(method, path, body=None); no mutation retries or redirects."""
    def __init__(self, token, *, opener=None):
        require(isinstance(token, str) and bool(token.strip()) and not any(c.isspace() for c in token), 'invalid-token')
        self._token = token
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self._last = 0.0
        self._deadline = time.monotonic() + 100
        self.calls = 0
    def request(self, method, path, body=None):
        page = bool(re.fullmatch('/pages/' + UUID_RE.pattern, path))
        require((method == 'GET' and (page or path in ('/data_sources/' + SOURCE_ID, '/databases/' + DB_ID))) or
                (method == 'POST' and path in {QUERY_PATH, '/pages'}) or
                (method == 'PATCH' and page), 'endpoint-not-allowed')
        if method == 'PATCH':
            require(isinstance(body, dict) and (set(body) == {'properties'} or
                    set(body) == {'in_trash'} and body['in_trash'] is True), 'patch-properties-only')
        if method == 'POST' and path == '/pages':
            require(isinstance(body, dict) and set(body) == {'parent', 'properties', 'template'} and
                    body['parent'] == {'type': 'data_source_id', 'data_source_id': SOURCE_ID} and
                    body['template'] == {'type': 'none'}, 'unsafe-create')
        self.calls += 1
        require(self.calls <= 1000, 'request-cap')
        raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        require(raw is None or len(raw) <= 500000, 'payload-size-cap')
        time.sleep(max(0, 0.4 - (time.monotonic() - self._last)))
        req = urllib.request.Request('https://api.notion.com/v1' + path, data=raw, method=method,
            headers={'Authorization': 'Bearer ' + self._token, 'Notion-Version': API_VERSION, 'Content-Type': 'application/json'})
        self._last = time.monotonic()
        require(time.monotonic() < self._deadline, 'run-time-budget')
        try:
            with self._opener.open(req, timeout=min(20, max(0.1, self._deadline - time.monotonic()))) as response:
                require(200 <= response.status < 300, 'http-status')
                data = response.read(16 * 1024 * 1024 + 1)
                require(len(data) <= 16 * 1024 * 1024, 'response-size-cap')
                result = strict_json(data)
                require(isinstance(result, dict), 'invalid-response')
                return result
        except urllib.error.HTTPError as exc:
            raise SyncError('http-' + str(exc.code)) from None
        except (OSError, ValueError):
            raise SyncError('transport-failure') from None

def query_all(api, cap=100):
    require(type(cap) is int and 1 <= cap <= 100, 'invalid-pagination-cap')
    pages, cursors, cursor = [], set(), None
    for _ in range(cap):
        body = {'page_size': 100}
        if cursor is not None:
            body['start_cursor'] = cursor
        result = api.request('POST', QUERY_PATH, body)
        require(isinstance(result, dict) and isinstance(result.get('results'), list) and
                len(result['results']) <= 100 and type(result.get('has_more')) is bool, 'invalid-query')
        pages.extend(result['results'])
        if not result['has_more']:
            require(result.get('next_cursor') is None, 'invalid-continuation')
            return pages
        cursor = result.get('next_cursor')
        require(isinstance(cursor, str) and 0 < len(cursor) <= 256 and cursor not in cursors, 'invalid-cursor')
        cursors.add(cursor)
    raise SyncError('incomplete-query')
