#!/usr/bin/env python3
"""find-url - Search Chrome bookmarks and history for URLs.

Port of upstream find-url.mjs to Python (no Node.js dependency).
Used to locate URLs that public search engines can't reach (internal systems, SSO portals, intranet domains).

Usage:
  python3 find-url.py [keywords...] [--only bookmarks|history] [--limit N] [--since 1d|7h|YYYY-MM-DD] [--sort recent|visits]

Examples:
  python3 find-url.py 财务小智
  python3 find-url.py agent skills
  python3 find-url.py github --since 7d --only history
  python3 find-url.py --since 2d --only history --sort visits
"""
import argparse
import datetime
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def get_chrome_data_dir() -> Path | None:
    """Get Chrome user data directory (cross-platform)."""
    home = Path.home()
    system = platform.system()
    if system == 'Darwin':
        return home / 'Library/Application Support/Google/Chrome'
    elif system == 'Linux':
        return home / '.config/google-chrome'
    elif system == 'Windows' or 'microsoft' in _read_proc_version():
        local_app_data = os.environ.get('LOCALAPPDATA', '')
        if local_app_data:
            return Path(local_app_data) / 'Google/Chrome/User Data'
    return None


def _read_proc_version() -> str:
    """Read /proc/version for WSL2 detection."""
    try:
        return Path('/proc/version').read_text().lower()
    except (OSError, FileNotFoundError):
        return ''


def list_profiles(data_dir: Path) -> list[dict]:
    """List Chrome profiles from Local State."""
    state_file = data_dir / 'Local State'
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text('utf-8'))
            info_cache = state.get('profile', {}).get('info_cache', {})
            profiles = [{'dir': d, 'name': v.get('name', d)} for d, v in info_cache.items()]
            if profiles:
                return profiles
        except (json.JSONDecodeError, OSError):
            pass
    return [{'dir': 'Default', 'name': 'Default'}]


def search_bookmarks(profile_dir: Path, profile_name: str, keywords: list[str]) -> list[dict]:
    """Search Chrome bookmarks for matching keywords (AND match)."""
    bookmarks_file = profile_dir / 'Bookmarks'
    if not bookmarks_file.exists() or not keywords:
        return []
    try:
        data = json.loads(bookmarks_file.read_text('utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    
    needles = [k.lower() for k in keywords]
    results = []
    
    def walk(node, trail):
        if not node:
            return
        if node.get('type') == 'url':
            hay = f"{node.get('name', '')} {node.get('url', '')}".lower()
            if all(n in hay for n in needles):
                results.append({
                    'profile': profile_name,
                    'name': node.get('name', ''),
                    'url': node.get('url', ''),
                    'folder': ' / '.join(trail),
                })
        children = node.get('children', [])
        if children:
            sub = [*trail, node['name']] if node.get('name') else trail
            for child in children:
                walk(child, sub)
    
    for root in (data.get('roots') or {}).values():
        walk(root, [])
    return results


def search_history(
    profile_dir: Path,
    profile_name: str,
    keywords: list[str],
    since: datetime.datetime | None,
    limit: int,
    sort: str,
) -> list[dict]:
    """Search Chrome history SQLite database for matching keywords."""
    history_file = profile_dir / 'History'
    if not history_file.exists():
        return []
    
    # Copy to temp (Chrome locks the original)
    tmp = Path(tempfile.mktemp(suffix='.sqlite'))
    try:
        shutil.copy2(history_file, tmp)
        
        # Check sqlite3 CLI availability
        if not shutil.which('sqlite3'):
            print('ERROR: sqlite3 not found. Install: apt install sqlite3 / brew install sqlite / winget install sqlite.sqlite',
                  file=sys.stderr)
            return []
        
        conds = ['last_visit_time > 0']
        for kw in keywords:
            esc = kw.lower().replace("'", "''")
            conds.append(f"LOWER(title || ' ' || url) LIKE '%{esc}%'")
        
        # WebKit epoch: 1601-01-01 00:00:00 UTC
        WEBKIT_EPOCH_DIFF_US = 11644473600000000
        if since:
            webkit_us = int(since.timestamp() * 1_000_000) + WEBKIT_EPOCH_DIFF_US
            conds.append(f'last_visit_time >= {webkit_us}')
        
        limit_clause = -1 if limit == 0 else limit * 2  # Fetch more for cross-profile merge
        order_by = 'visit_count DESC, last_visit_time DESC' if sort == 'visits' else 'last_visit_time DESC'
        
        sql = (
            f"SELECT title, url, "
            f"datetime((last_visit_time - 11644473600000000)/1000000, 'unixepoch', 'localtime') AS visit, "
            f"visit_count FROM urls WHERE {' AND '.join(conds)} "
            f"ORDER BY {order_by} LIMIT {limit_clause};"
        )
        
        result = subprocess.run(
            ['sqlite3', '-separator', '\t', str(tmp), sql],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        
        rows = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 4:
                rows.append({
                    'profile': profile_name,
                    'title': parts[0],
                    'url': parts[1],
                    'visit': parts[2],
                    'visit_count': int(parts[3]) if parts[3].isdigit() else 0,
                })
        return rows
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f'ERROR: History search failed: {e}', file=sys.stderr)
        return []
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def parse_since(value: str) -> datetime.datetime:
    """Parse --since value: 1d / 7h / 30m / YYYY-MM-DD."""
    import re
    m = re.match(r'^(\d+)([dhm])$', value)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {'d': datetime.timedelta(days=n), 'h': datetime.timedelta(hours=n), 'm': datetime.timedelta(minutes=n)}[unit]
        return datetime.datetime.now() - delta
    try:
        return datetime.datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid --since value: {value} (use 1d / 7h / 30m / YYYY-MM-DD)")


def clean_text(s) -> str:
    """Clean text for pipe-delimited output."""
    return str(s or '').replace('|', '│').strip()


def main():
    parser = argparse.ArgumentParser(
        description='Search Chrome bookmarks and history for URLs',
        epilog='Examples: find-url.py github --since 7d --only history',
    )
    parser.add_argument('keywords', nargs='*', help='Search keywords (AND match)')
    parser.add_argument('--only', choices=['bookmarks', 'history'], help='Limit to one source')
    parser.add_argument('--limit', type=int, default=20, help='Max results (0=unlimited)')
    parser.add_argument('--since', type=parse_since, help='Time window: 1d / 7h / 30m / YYYY-MM-DD')
    parser.add_argument('--sort', choices=['recent', 'visits'], default='recent', help='Sort order for history')
    args = parser.parse_args()
    
    data_dir = get_chrome_data_dir()
    if not data_dir or not data_dir.exists():
        print('ERROR: Chrome user data directory not found', file=sys.stderr)
        sys.exit(1)
    
    profiles = list_profiles(data_dir)
    do_bookmarks = args.only != 'history'
    do_history = args.only != 'bookmarks'
    
    all_bookmarks = []
    all_history = []
    
    for p in profiles:
        p_dir = data_dir / p['dir']
        if not p_dir.exists():
            continue
        if do_bookmarks:
            all_bookmarks.extend(search_bookmarks(p_dir, p['name'], args.keywords))
        if do_history:
            all_history.extend(search_history(p_dir, p['name'], args.keywords, args.since, args.limit, args.sort))
    
    # Sort history across profiles
    if args.sort == 'visits':
        all_history.sort(key=lambda x: (-(x.get('visit_count') or 0), x.get('visit', '')), reverse=False)
    else:
        all_history.sort(key=lambda x: x.get('visit', ''), reverse=True)
    
    # Apply limit
    bookmarks_out = all_bookmarks if args.limit == 0 else all_bookmarks[:args.limit]
    history_out = all_history if args.limit == 0 else all_history[:args.limit]
    
    # Multi-profile annotation
    seen_profiles = {x['profile'] for x in bookmarks_out + history_out}
    show_profile = len(seen_profiles) > 1
    
    # Output
    if do_bookmarks and bookmarks_out:
        print(f'[Bookmarks] {len(bookmarks_out)} results')
        for b in bookmarks_out:
            segs = [clean_text(b['name']) or '(no title)', clean_text(b['url'])]
            if b.get('folder'):
                segs.append(clean_text(b['folder']))
            if show_profile:
                segs.append('@' + clean_text(b['profile']))
            print('  ' + ' | '.join(segs))
    
    if do_bookmarks and do_history and bookmarks_out and history_out:
        print()
    
    if do_history and history_out:
        sort_label = 'by visits' if args.sort == 'visits' else 'by recent'
        print(f'[History] {len(history_out)} results ({sort_label})')
        for h in history_out:
            segs = [clean_text(h['title']) or '(no title)', clean_text(h['url']), h.get('visit', '')]
            if h.get('visit_count', 0) > 1:
                segs.append(f'visits={h["visit_count"]}')
            if show_profile:
                segs.append('@' + clean_text(h['profile']))
            print('  ' + ' | '.join(segs))
    
    if not args.keywords and do_bookmarks and not do_history:
        print('\nTip: Bookmarks have no time dimension, searching without keywords is meaningless. Add keywords or use --only history.', file=sys.stderr)


if __name__ == '__main__':
    main()