"""Scan an actual extracted delivery or ZIP. --tracked checks only source candidates.
Exit codes: 0 clean, 1 findings, 2 incomplete/invalid scan. No matched value is logged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import zipfile

MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILES = 10000
BANNED_DIRS = {'.git', '.git-broken', '.venv', 'node_modules', '__pycache__',
               '.pytest_cache', 'outputs', '.codex', '.workbuddy'}
PLACEHOLDERS = {'', 'your_api_key', 'your_key_here', 'your-api-key', 'replace_me',
                'changeme', '<your-api-key>', '${INTERN_API_KEY}'}
PATTERNS = [
    ('credential assignment', re.compile(r'''(?ix)["']?\b(?:[a-z0-9_]*(?:api_key|access_token|auth_token|password|secret))["']?\s*[:=]\s*["']([^"'\r\n]*)["']''')),
    ('unquoted credential', re.compile(r'(?im)^\s*(?:INTERN_API_KEY|[A-Z0-9_]*(?:API_KEY|TOKEN|PASSWORD|SECRET))\s*=\s*([^\s\x22\x27#]+)')),
    ('GitHub token', re.compile(r'\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b')),
    ('API token', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
    ('Bearer token', re.compile(r'(?i)\bBearer\s+([A-Za-z0-9_.-]{20,})')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
]

class ScanError(ValueError):
    pass


def forbidden(name):
    parts = PurePosixPath(name.replace('\\', '/')).parts
    if not parts or name.startswith(('/', '\\')) or any(p in ('..', '.') or ':' in p for p in parts):
        return True
    base = parts[-1].lower()
    return (any(p.lower() in BANNED_DIRS or p.lower().startswith('.git-broken') for p in parts)
            or (base.startswith('.env') and base != '.env.example')
            or bool(re.search(r'\.(?:db|sqlite\d*|log|jsonl)(?:-(?:wal|shm|journal))?$', base)))


def scan_content(name, data):
    # Decode text without silently dropping bytes. Binary ASCII tokens remain detectable.
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        content = data.decode('utf-16')
    else:
        try:
            content = data.decode('utf-8-sig')
        except UnicodeError:
            try:
                content = data.decode('gb18030')
            except UnicodeError:
                content = data.decode('latin-1')
    findings = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(content):
            value = match.group(1) if match.lastindex else match.group()
            if value.lower() in {p.lower() for p in PLACEHOLDERS}:
                continue
            findings.append({'path': name, 'line': content.count('\n', 0, match.start()) + 1,
                             'kind': kind, 'value': '[REDACTED]'})
    return findings


def entries(path, tracked=False):
    if not path.exists():
        raise ScanError('Input does not exist')
    if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
        raise ScanError('Symbolic input is not supported')
    if path.is_dir():
        if tracked:
            proc = subprocess.run(['git', 'ls-files', '-z'], cwd=path, capture_output=True)
            if proc.returncode:
                raise ScanError('Cannot enumerate tracked source files')
            names = [os.fsdecode(n) for n in proc.stdout.split(b'\0') if n]
            paths = [path / n for n in names]
        else:
            def walk(folder):
                # pathlib.rglob may suppress directory-read errors: never use it for
                # a fail-closed package scan. Do not descend through links/junctions.
                with os.scandir(folder) as iterator:
                    children = sorted(iterator, key=lambda e:e.name)
                for entry in children:
                    child=Path(entry.path)
                    yield child
                    if entry.is_dir(follow_symlinks=False) and not (hasattr(child,'is_junction') and child.is_junction()):
                        yield from walk(child)
            paths = walk(path)
        for file in paths:
            if tracked and not file.resolve().is_relative_to(path.resolve()):
                raise ScanError('Tracked member escapes the source root')
            if file.is_symlink() or (hasattr(file, 'is_junction') and file.is_junction()):
                raise ScanError('Symbolic links/junctions are not accepted')
            if file.is_dir():
                if forbidden(file.relative_to(path).as_posix()):
                    yield file.relative_to(path).as_posix()+'/', b''
                continue
            if not file.is_file() or file.stat().st_size > MAX_FILE:
                raise ScanError('Unreadable, unsupported or oversized entry')
            yield file.relative_to(path).as_posix(), file.read_bytes()
    elif not tracked and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            seen = set()
            for entry in archive.infolist():
                name = entry.filename
                normalized = name.replace('\\', '/').casefold().rstrip('/')
                if normalized in seen:
                    raise ScanError('Duplicate archive member')
                seen.add(normalized)
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise ScanError('Symbolic/encrypted archive member')
                if entry.file_size > MAX_FILE:
                    raise ScanError('Oversized archive member')
                if entry.is_dir():
                    if forbidden(name):
                        yield name, b''
                    continue
                yield name, archive.read(entry)
    else:
        raise ScanError('Expected a directory or readable ZIP archive')


def scan(path, tracked=False):
    report = {'scope': 'tracked-source-candidate' if tracked else 'actual-package',
              'files_scanned': 0, 'bytes_scanned': 0, 'manifest': [], 'findings': [], 'errors': []}
    try:
        for name, data in entries(Path(path), tracked):
            report['files_scanned'] += 1
            report['bytes_scanned'] += len(data)
            if report['files_scanned'] > MAX_FILES or report['bytes_scanned'] > MAX_TOTAL:
                raise ScanError('Package exceeds scan limits')
            report['manifest'].append({'path': name, 'sha256': hashlib.sha256(data).hexdigest()})
            if forbidden(name):
                report['findings'].append({'path': name, 'kind': 'private/runtime path'})
            report['findings'].extend(scan_content(name, data))
        if not report['files_scanned']:
            raise ScanError('Empty scan cannot establish delivery safety')
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        # Exception text may contain a secret from a malicious filename or decoder.
        report['errors'].append({'kind': type(exc).__name__, 'message': 'Scan incomplete; inspect input permissions, format and limits'})
    report['exit_code'] = 2 if report['errors'] else (1 if report['findings'] else 0)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True)
    parser.add_argument('--tracked', action='store_true', help='Only tracked source candidates; does not certify a package')
    parser.add_argument('--strict', action='store_true', help='Compatibility option; findings always fail')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    report = scan(Path(args.path).absolute(), args.tracked)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    if args.report:
        try:
            args.report.write_text(payload+'\n', encoding='utf-8')
        except OSError:
            print('Cannot write scan report', file=sys.stderr)
            raise SystemExit(2)
    print(payload)
    raise SystemExit(report['exit_code'])

if __name__ == '__main__':
    main()
