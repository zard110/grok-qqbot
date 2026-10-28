#!/usr/bin/env python3
"""Scoped knowledge CLI; legacy search/sync/list/add-doc flags remain available."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse
import groups as G
from core.files import atomic_write_bytes, atomic_write_json, confined_path, file_lock
from core.memory import MemoryStore
from core.settings import DATA_DIR
MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
TEXT_TYPES = {'.md', '.txt', '.py', '.js', '.ts', '.tsx', '.json', '.yaml', '.yml', '.rst'}


def _read_bounded(path, limit):
    if path.stat().st_size > limit:
        raise ValueError('Source exceeds read budget')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Source grew beyond read budget')
    return data


def kdir(goid, cfg):
    path = confined_path(G.group_dir(goid), (cfg.get('knowledge') or {}).get('docs_dir') or 'knowledge')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _files(kd):
    for path in sorted(kd.rglob('*')):
        relative = path.relative_to(kd)
        if any(p.startswith('.') or p == 'node_modules' for p in relative.parts):
            continue
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in TEXT_TYPES | {'.pdf'}:
            yield relative.as_posix(), confined_path(kd, relative)


def _pdf_text(kd, path):
    relative = path.relative_to(kd).as_posix()
    digest = hashlib.sha256(_read_bounded(path, MAX_BYTES)).hexdigest()
    cache_root = confined_path(kd, '.pdftext')
    cache_root.mkdir(exist_ok=True)
    cache = cache_root / (hashlib.sha256(relative.encode()).hexdigest() + '.txt')
    meta = cache.with_suffix('.json')
    with file_lock(cache.with_suffix('.lock')):
        try:
            if json.loads(meta.read_text())['digest'] == digest and cache.stat().st_size <= MAX_TEXT:
                return cache.read_text(encoding='utf-8')
        except (OSError, ValueError, KeyError):
            pass
        fd, temporary = tempfile.mkstemp(prefix='.extract-', dir=cache_root)
        os.close(fd)
        temp_path, process = Path(temporary), None
        try:
            process = subprocess.Popen(['pdftotext', '-layout', str(path), str(temp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while process.poll() is None:
                if time.monotonic() > deadline or temp_path.stat().st_size > MAX_TEXT:
                    process.kill(); process.wait(timeout=5)
                    raise ValueError('PDF extraction exceeded time or output budget')
                time.sleep(.05)
            if process.returncode or temp_path.stat().st_size > MAX_TEXT:
                raise ValueError('PDF extraction failed')
            text = temp_path.read_text(encoding='utf-8')
            if hashlib.sha256(_read_bounded(path, MAX_BYTES)).hexdigest() != digest:
                raise ValueError('PDF changed during extraction; retry')
            atomic_write_bytes(cache, text.encode())
            atomic_write_json(meta, {'digest': digest, 'source': relative})
            return text
        finally:
            if process and process.poll() is None:
                process.kill(); process.wait(timeout=5)
            temp_path.unlink(missing_ok=True)


def pdf_cache(kd):
    for _, path in _files(kd):
        if path.suffix.lower() == '.pdf':
            if path.stat().st_size > MAX_BYTES:
                raise ValueError('PDF exceeds 20 MiB')
            _pdf_text(kd, path)


def refresh_index(goid, cfg, store=None):
    store, kd = store or MemoryStore(DATA_DIR), kdir(goid, cfg)
    with file_lock(G.group_dir(goid) / '.knowledge.lock'):
        seen, failures = set(), []
        for name, path in _files(kd):
            seen.add(name)
            try:
                if path.stat().st_size > MAX_BYTES:
                    raise ValueError('Document exceeds 20 MiB')
                text = _pdf_text(kd, path) if path.suffix.lower() == '.pdf' else _read_bounded(path, MAX_TEXT).decode('utf-8')
                store.index_document(goid, name, text)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                store.delete_document(goid, name)
                failures.append({'file': name, 'error': str(error)})
        for record in store.documents(goid):
            if record['name'] not in seen:
                store.delete_document(goid, record['name'])
        cache = confined_path(kd, '.pdftext')
        if cache.exists():
            for metadata in cache.glob('*.json'):
                try:
                    if json.loads(metadata.read_text())['source'] not in seen:
                        metadata.with_suffix('.txt').unlink(missing_ok=True)
                        metadata.unlink()
                except (OSError, ValueError, KeyError):
                    continue
    return failures


def cmd_search(goid, cfg, args):
    store = MemoryStore(DATA_DIR)
    failures = refresh_index(goid, cfg, store)
    hits = store.search_documents(goid, args.query, args.max)
    context = max(0, min(int(args.context), 20))
    for hit in hits:
        lines = hit['text'].splitlines()
        terms = [args.query.lower()] + args.query.lower().split()
        index = next((i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)), 0)
        start = max(0, index - context)
        hit['text'] = '\n'.join(lines[start:index + context + 1])
        hit['line'] += start
    if args.json:
        print(json.dumps({'group': cfg.get('name'), 'query': args.query, 'hits': hits, 'errors': failures}, ensure_ascii=False, indent=1))
    else:
        for hit in hits:
            print(f"{hit['file']}:{hit['line']} {hit['text']}")
        for failure in failures:
            print(f"ERROR {failure['file']}: {failure['error']}")
    return 2 if failures else 0 if hits else 1


def cmd_sync(goid, cfg, args):
    gd, kd, rc = G.group_dir(goid), kdir(goid, cfg), 0
    for repo in (cfg.get('knowledge') or {}).get('repos') or []:
        url = repo.get('url', '')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password or parsed.query or parsed.fragment or len(parsed.path.strip('/').split('/')) != 2:
            raise ValueError('Sync accepts public https://github.com/<owner>/<repository> URLs only')
        dst = confined_path(gd, repo.get('local_path') or 'knowledge/repos/' + Path(parsed.path).stem)
        if not dst.is_relative_to(kd):
            raise ValueError('Repository must stay inside configured knowledge directory')
        with file_lock(gd / '.knowledge.lock'):
            if (dst / '.git').exists():
                remote = subprocess.run(['git', '-C', str(dst), 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
                if remote != url:
                    raise ValueError('Existing remote does not match configuration')
                cmd = ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(dst), 'pull', '--ff-only', '-q']
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                branch = repo.get('branch') or ''
                cmd = ['git', '-c', 'core.hooksPath=/dev/null', 'clone', '-q', '--depth', '1'] + (['-b', branch] if branch else []) + ['--', url, str(dst)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
            print(f"{'OK' if result.returncode == 0 else 'ERR'} {url}")
            rc = max(rc, int(result.returncode != 0))
    return max(rc, int(bool(refresh_index(goid, cfg))))


def cmd_list(goid, cfg, args):
    for name, path in _files(kdir(goid, cfg)):
        print(f'doc {name} {path.stat().st_size}B')
    return 0


def cmd_add_doc(goid, cfg, args):
    source = Path(args.path)
    if source.suffix.lower() not in {'.md', '.txt', '.pdf'} or source.stat().st_size > MAX_BYTES:
        raise ValueError('Only md/txt/pdf documents up to 20 MiB accepted')
    with file_lock(G.group_dir(goid) / '.knowledge.lock'):
        MemoryStore(DATA_DIR).delete_document(goid, source.name)
        atomic_write_bytes(confined_path(kdir(goid, cfg), source.name), _read_bounded(source, MAX_BYTES))
    failures = refresh_index(goid, cfg)
    if failures:
        raise ValueError(json.dumps(failures, ensure_ascii=False))
    print(f'added {source.name}')
    return 0


def main():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument('--group', default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__, parents=[parent])
    parser.set_defaults(group=None)
    sub = parser.add_subparsers(dest='cmd', required=True)
    search = sub.add_parser('search', parents=[parent]); search.add_argument('query')
    search.add_argument('--max', type=int, default=20); search.add_argument('--context', type=int, default=1); search.add_argument('--json', action='store_true')
    for name in ('sync', 'list', 'import-legacy', 'context'):
        sub.add_parser(name, parents=[parent])
    for name in ('add-doc', 'remove', 'export', 'import'):
        sub.add_parser(name, parents=[parent]).add_argument('path')
    rollback = sub.add_parser('rollback', parents=[parent]); rollback.add_argument('path'); rollback.add_argument('revision', type=int)
    args = parser.parse_args()
    try:
        goid = G.resolve(args.group); cfg = G.load_config(goid)
        if not cfg or not cfg.get('enabled', True) or not cfg.get('features', {}).get('knowledge', True):
            raise ValueError('Group knowledge unavailable or disabled')
        function = {'search': cmd_search, 'sync': cmd_sync, 'list': cmd_list, 'add-doc': cmd_add_doc}.get(args.cmd)
        if function:
            return function(goid, cfg, args)
        store = MemoryStore(DATA_DIR)
        if args.cmd == 'remove':
            store.delete_document(goid, args.path)
            confined_path(kdir(goid, cfg), args.path).unlink(missing_ok=True)
            refresh_index(goid, cfg, store)
        elif args.cmd == 'export':
            atomic_write_json(Path(args.path), store.export_scope(goid))
        elif args.cmd == 'import':
            snapshot = json.loads(Path(args.path).read_text(encoding='utf-8'))
            if snapshot.get('scope') != goid or snapshot.get('format') != 1:
                raise ValueError('Snapshot scope or format mismatch')
            staged = []
            for doc in snapshot.get('documents', []):
                name = doc['name'] if Path(doc['name']).suffix.lower() in TEXT_TYPES else doc['name'] + '.restored.txt'
                target = confined_path(kdir(goid, cfg), name)
                if not isinstance(doc['text'], str) or len(doc['text'].encode()) > MAX_TEXT:
                    raise ValueError('Snapshot document exceeds text budget')
                staged.append((name, target, doc['text']))
            result = store.restore_scope(goid, {**snapshot, 'documents': []})
            with file_lock(G.group_dir(goid) / '.knowledge.lock'):
                for name, target, text in staged:
                    # Import never overwrites a concurrently maintained source document.
                    if target.exists():
                        continue
                    atomic_write_bytes(target, text.encode())
                    store.index_document(goid, name, text)
            print({**result, 'document_candidates': len(staged)})
        elif args.cmd == 'rollback':
            if Path(args.path).suffix.lower() == '.pdf':
                raise ValueError('Restore original PDF bytes; extracted text cannot restore a PDF')
            result = store.rollback_document(goid, args.path, args.revision)
            document = next(d for d in store.export_scope(goid)['documents'] if d['name'] == args.path)
            atomic_write_bytes(confined_path(kdir(goid, cfg), args.path), document['text'].encode())
            print(result)
        elif args.cmd == 'import-legacy':
            gd = G.group_dir(goid)
            print(store.import_legacy(goid, gd / 'last-events.jsonl', gd / 'notes.md', gd / 'sent-log.jsonl'))
        else:
            print(json.dumps(store.context(goid), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}'); return 2


if __name__ == '__main__':
    raise SystemExit(main())
