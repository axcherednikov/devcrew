"""Read-only memory inventory and calibration from measured observations."""
import devcrew_fs as fs
import hashlib
import json
import math
import os
import pathlib
import stat


def _walk_error(error):
    raise error


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def audit(project, relative, max_files, max_bytes):
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError('memory thresholds must be positive')
    root = project
    for part in pathlib.PurePosixPath(relative).parts:
        root = root / part
        if fs.is_link(root):
            raise ValueError('memory directory must not traverse symlinks')
    files, skipped, by_hash = [], [], {}
    if root.exists() and not root.is_dir():
        raise ValueError('memory path must be a directory')
    for directory, dirs, names in (os.walk(root, followlinks=False, onerror=_walk_error) if root.exists() else ()):
        for name in sorted(dirs + names):
            path = pathlib.Path(directory) / name
            rel = path.relative_to(project).as_posix()
            info = path.lstat()
            if fs.is_link(path):
                skipped.append(rel)
                if name in dirs:
                    dirs.remove(name)
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                skipped.append(rel)
                continue
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    digest.update(chunk)
            sha = digest.hexdigest()
            files.append({'path': rel, 'bytes': info.st_size, 'sha256': sha})
            by_hash.setdefault(sha, []).append(rel)
    total = sum(item['bytes'] for item in files)
    return {'files': sorted(files, key=lambda item: item['path']), 'bytes': total,
            'duplicates': sorted(sorted(paths) for paths in by_hash.values() if len(paths) > 1),
            'skipped': sorted(skipped), 'warnings': [message for condition, message in (
                (len(files) > max_files, 'file-count threshold exceeded'),
                (total > max_bytes, 'byte-count threshold exceeded')) if condition]}


def summarize(path):
    groups, excluded, seen = {}, [], set()
    with pathlib.Path(path).open(encoding='utf-8') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            fields = ('task', 'unit', 'coverage', 'estimate_scope', 'actual_scope', 'estimate_model', 'actual_model')
            if not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip() for key in fields):
                raise ValueError(f'line {line_no}: nonempty observation identifiers required')
            if row['unit'] not in ('hours', 'minutes', 'tokens'):
                raise ValueError(f'line {line_no}: unsupported unit')
            for key in ('estimate', 'actual'):
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value) or value < 0 or (key == 'estimate' and value == 0):
                    raise ValueError(f'line {line_no}: invalid {key}')
            identity = tuple(row[key] for key in ('task', 'unit', 'coverage', 'estimate_scope', 'estimate_model'))
            if identity in seen:
                raise ValueError(f'line {line_no}: duplicate observation')
            seen.add(identity)
            reasons = [key for key in ('scope', 'model') if row['estimate_' + key] != row['actual_' + key]]
            if reasons:
                excluded.append({'task': row['task'], 'reasons': reasons})
                continue
            key = (row['unit'], row['coverage'], row['actual_model'])
            group = groups.setdefault(key, {'unit': key[0], 'coverage': key[1], 'model': key[2], 'count': 0, 'estimate': 0, 'actual': 0})
            group['count'] += 1
            for field in ('estimate', 'actual'):
                group[field] += row[field]
                if not _finite(group[field]):
                    raise ValueError('observation totals overflow')
    for group in groups.values():
        group['actual_to_estimate'] = group['actual'] / group['estimate']
        if not _finite(group['actual_to_estimate']):
            raise ValueError('observation ratio overflow')
    return {'groups': [groups[key] for key in sorted(groups)], 'excluded': excluded}
