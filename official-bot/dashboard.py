#!/usr/bin/env python3
"""Loopback administration UI. Run: python dashboard.py --port 8787.

The one-use login code is printed to the launching terminal, never placed in URLs.
Use an authenticated TLS tunnel for remote administration; this server is local only.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import groups as G
import kb
from core.files import atomic_write_text, atomic_write_bytes, confined_path, file_lock
from core.memory import MemoryStore, MemoryConflict
from core.settings import DATA_DIR

STATIC = Path(__file__).parent / 'dashboard' / 'index.html'
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}


def bounded_read(path, limit):
    if not path.exists():
        return b''
    if path.stat().st_size > limit:
        raise ValueError('File exceeds read budget')
    with path.open('rb') as stream:
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ValueError('File exceeded read budget while reading')
    return result


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port=8787, data_dir=DATA_DIR, bootstrap=None):
        self.data_dir = Path(data_dir)
        self.store = MemoryStore(self.data_dir)
        self.bootstrap = bootstrap or secrets.token_urlsafe(24)
        self.sessions = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.jobs = {}
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.cookie = 'qqbot_admin_' + secrets.token_hex(6)

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def job(self, function):
        with self.lock:
            if sum(j['state'] in ('queued', 'running') for j in self.jobs.values()) >= 4:
                raise ValueError('Too many pending jobs')
            job_id = secrets.token_hex(12)
            self.jobs[job_id] = {'id': job_id, 'state': 'queued', 'created_at': time.time()}
            for key in list(self.jobs)[:-100]:
                if self.jobs[key]['state'] not in ('queued', 'running'):
                    del self.jobs[key]
        def run():
            with self.lock:
                self.jobs[job_id]['state'] = 'running'
            try:
                result = function()
                with self.lock:
                    failed = (isinstance(result, list) and bool(result)) or (type(result) is int and result != 0)
                    self.jobs[job_id].update(state='failed' if failed else 'complete', result=result)
            except Exception as error:
                with self.lock:
                    self.jobs[job_id].update(state='failed', error=type(error).__name__)
        self.pool.submit(run)
        return {'job_id': job_id}


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        # No access logs: request URLs and authentication material stay private.
        pass

    def reply(self, status, body, content_type='application/json; charset=utf-8', cookie=None):
        content = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(content)

    def guard(self, mutation=False):
        if self.headers.get('Host') != self.server.origin.removeprefix('http://'):
            raise PermissionError('Invalid Host')
        origin = self.headers.get('Origin')
        if (origin and origin != self.server.origin) or (mutation and origin != self.server.origin):
            raise PermissionError('Invalid Origin')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise PermissionError('Cross-site request rejected')

    def session(self, mutation=False):
        jar = SimpleCookie()
        jar.load(self.headers.get('Cookie', ''))
        item = jar.get(self.server.cookie)
        key = hashlib.sha256(item.value.encode()).hexdigest() if item else ''
        with self.server.lock:
            session = self.server.sessions.get(key)
            if not session or session['expires'] <= time.time():
                self.server.sessions.pop(key, None)
                raise PermissionError('Login required')
        if mutation and not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            raise PermissionError('Invalid CSRF token')
        return session

    def read_json(self):
        if self.headers.get_content_type() != 'application/json':
            raise ValueError('Expected application/json')
        size = int(self.headers.get('Content-Length', '0'))
        if size < 1 or size > 5 * 1024 * 1024:
            raise ValueError('Request exceeds 5 MiB or has no body')
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict):
            raise ValueError('Expected JSON object')
        return data

    def dispatch(self, mutation=False):
        try:
            self.guard(mutation)
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if not mutation and path == '/':
                return self.reply(200, STATIC.read_bytes(), 'text/html; charset=utf-8')
            body = self.read_json() if mutation else {}
            if mutation and path == '/api/login':
                with self.server.lock:
                    if not self.server.bootstrap or not hmac.compare_digest(str(body.get('code', '')), self.server.bootstrap):
                        raise PermissionError('Invalid or used login code')
                    self.server.bootstrap = None
                    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                    self.server.sessions[hashlib.sha256(raw.encode()).hexdigest()] = {'csrf': csrf, 'expires': time.time() + 12 * 3600}
                return self.reply(200, {'csrf': csrf}, cookie=f'{self.server.cookie}={raw}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
            session = self.session(mutation)
            if path == '/api/session' and not mutation:
                return self.reply(200, {'csrf': session['csrf']})
            if path == '/api/groups' and not mutation:
                return self.reply(200, G.all_groups())
            if path == '/api/plugins' and not mutation:
                from core.plugins import default_registry
                return self.reply(200, default_registry.list_plugins())
            if path == '/api/health' and not mutation:
                result = {}
                for name in ('health.json', 'local-watchdog-state.json', 'supervisor-bridge.json', 'supervisor-watchdog.json'):
                    try:
                        data = json.loads((self.server.data_dir / name).read_text())
                        result[name] = {k: v for k, v in data.items() if k in ('gateway_connected', 'last_ack_at', 'last_event_at', 'queue_depth', 'last_dispatch_at', 'last_check', 'last_ok', 'consecutive_failures', 'last_action', 'pid', 'updated_at', 'last_live', 'unhealthy_since', 'last_check_ts', 'bridge_pid', 'last_live_age', 'gateway_status', 'status', 'role', 'child_pid', 'started_at', 'retry_at', 'exit_code')}
                    except (OSError, ValueError, AttributeError):
                        result[name] = {'available': False}
                try:
                    from core.delivery import snapshot
                    result['delivery'] = snapshot(self.server.data_dir / 'runtime.sqlite3')
                except Exception:
                    result['delivery'] = {'available': False}
                with self.server.lock:
                    result['jobs'] = list(self.server.jobs.values())
                return self.reply(200, result)
            parts = path.strip('/').split('/')
            if len(parts) < 4 or parts[:2] != ['api', 'groups']:
                return self.reply(404, {'error': 'Not found'})
            goid = parts[2]
            if not G.OPENID_RE.fullmatch(goid):
                raise ValueError('Expected canonical group ID')
            cfg = G.load_config(goid)
            if cfg is None:
                return self.reply(404, {'error': 'Group not found'})
            resource = '/'.join(parts[3:])
            gd = G.group_dir(goid)
            store = self.server.store
            if mutation and resource in ('config', 'plugin') and type(body.get('revision')) is not int:
                raise ValueError('Expected integer configuration revision')
            if resource == 'config':
                result = G.update_config(goid, expected_revision=body.get('revision'), **body.get('patch', {})) if mutation else cfg
            elif resource == 'plugin' and mutation:
                result = G.update_plugin_binding(goid, body['plugin_id'], body['binding'], expected_revision=body.get('revision'))
            elif resource == 'prompt':
                target = confined_path(gd, 'prompt.md')
                with file_lock(gd / '.prompt.lock'):
                    current = target.read_text(encoding='utf-8') if target.exists() else ''
                    revision = hashlib.sha256(current.encode()).hexdigest()
                    if mutation:
                        if body.get('revision') != revision:
                            raise MemoryConflict('Prompt changed; reload first')
                        text = body.get('text')
                        if not isinstance(text, str) or len(text) > 20000:
                            raise ValueError('Prompt exceeds 20000 characters')
                        atomic_write_text(target, text)
                        current, revision = text, hashlib.sha256(text.encode()).hexdigest()
                    result = {'text': current, 'revision': revision}
            elif resource == 'memories':
                result = store.save_memory(goid, body['subject'], body['predicate'], body['value'], memory_id=body.get('id'), expected_revision=body.get('revision')) if mutation else store.search_memories(goid, query.get('q', [''])[0])
            elif resource == 'memories/delete' and mutation:
                store.delete_memory(goid, body['id'], body['revision']); result = {'ok': True}
            elif resource == 'messages' and not mutation:
                result = store.messages(goid, limit=int(query.get('limit', ['100'])[0]), after_seq=int(query.get('after', ['0'])[0]))
            elif resource == 'context' and not mutation:
                result = store.context(goid)
            elif resource == 'summaries':
                result = store.create_summary(goid, body['from_seq'], body['to_seq'], body.get('text'), body.get('revision')) if mutation else store.summaries(goid)
            elif resource == 'import' and mutation:
                result = store.import_legacy(goid, confined_path(gd, 'last-events.jsonl'), confined_path(gd, 'notes.md'), confined_path(gd, 'sent-log.jsonl'))
            elif resource == 'export' and not mutation:
                result = store.export_scope(goid)
            elif resource == 'knowledge' and not mutation:
                # Serialize the source/index snapshot with editors, but never mutate
                # index state on GET. Explicit refresh retires missing documents.
                with file_lock(gd / '.knowledge.lock'):
                    result = store.search_documents(goid, query['q'][0]) if query.get('q') else store.documents(goid)
                    root = kb.kdir(goid, cfg)
                    result = [item for item in result if confined_path(root, item.get('file', item.get('name'))).is_file()]
                    if not query.get('q'):
                        for item in result:
                            target = confined_path(root, item['name'])
                            if target.stat().st_size <= kb.MAX_BYTES:
                                item['source_revision'] = hashlib.sha256(bounded_read(target, kb.MAX_BYTES)).hexdigest()
            elif resource == 'knowledge/refresh' and mutation:
                result = self.server.job(lambda: kb.refresh_index(goid, cfg, store))
            elif resource == 'knowledge/sync' and mutation:
                result = self.server.job(lambda: kb.cmd_sync(goid, cfg, None))
            elif resource == 'knowledge/save' and mutation:
                name = body['name']
                if Path(name).suffix.lower() not in ('.md', '.txt'):
                    raise ValueError('Inline document editor accepts md/txt')
                target = confined_path(kb.kdir(goid, cfg), name)
                text = body['text']
                if not isinstance(text, str) or len(text) > 200000:
                    raise ValueError('Document exceeds editor budget')
                with file_lock(gd / '.knowledge.lock'):
                    current = bounded_read(target, 200000)
                    if body.get('revision', '') != hashlib.sha256(current).hexdigest():
                        raise MemoryConflict('Document changed; reload first')
                    store.delete_document(goid, name)
                    atomic_write_text(target, text)
                    try:
                        result = store.index_document(goid, name, text)
                    except Exception as error:
                        raise ValueError('Source saved, but indexing failed. Old index is disabled; refresh the index to recover.') from error
            elif resource == 'knowledge/source' and not mutation:
                name = query['name'][0]
                if Path(name).suffix.lower() not in ('.md', '.txt'):
                    raise ValueError('Only md/txt editable')
                target = confined_path(kb.kdir(goid, cfg), name)
                current = bounded_read(target, 200000)
                result = {'name': name, 'text': current.decode('utf-8'), 'revision': hashlib.sha256(current).hexdigest()}
            elif resource == 'knowledge/delete' and mutation:
                with file_lock(gd / '.knowledge.lock'):
                    target = confined_path(kb.kdir(goid, cfg), body['name'])
                    current = bounded_read(target, kb.MAX_BYTES)
                    if body.get('revision') != hashlib.sha256(current).hexdigest():
                        raise MemoryConflict('Document changed; reload before deleting')
                    store.delete_document(goid, body['name'])
                    target.unlink(missing_ok=True)
                result = {'ok': True}
            elif resource == 'assets' and not mutation:
                result = self.assets(gd)
            elif resource == 'asset' and not mutation:
                item = next((a for a in self.assets(gd) if a['id'] == query.get('id', [''])[0]), None)
                if not item:
                    return self.reply(404, {'error': 'Asset not found'})
                target = confined_path(gd / 'stickers', item['name'])
                return self.reply(200, bounded_read(target, 20 * 1024 * 1024), mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
            else:
                return self.reply(404, {'error': 'Not found'})
            self.reply(200, result)
        except PermissionError as error:
            self.reply(403, {'error': str(error)})
        except (MemoryConflict, G.ConfigConflict) as error:
            self.reply(409, {'error': str(error)})
        except (ValueError, KeyError, TypeError, OSError) as error:
            self.reply(400, {'error': str(error)[:300]})
        except Exception:
            self.reply(500, {'error': 'Operation failed; inspect local service state'})

    def assets(self, gd):
        root = confined_path(gd, 'stickers')
        result = []
        if root.exists():
            for path in sorted(root.rglob('*')):
                if len(result) >= 500:
                    break
                if path.is_symlink() or path.suffix.lower() not in IMAGE_TYPES or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                    continue
                relative = path.relative_to(root).as_posix()
                confined_path(root, relative)
                result.append({'id': hashlib.sha256(relative.encode()).hexdigest(), 'name': relative, 'size': path.stat().st_size})
        return result

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    server = DashboardServer(args.port)
    print('Dashboard:', server.origin, flush=True)
    print('One-time login code (keep private):', server.bootstrap, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
