#!/usr/bin/env python3
"""match-site - Match user input against site experience files.

Port of upstream match-site.mjs to Python (no Node.js dependency).
Searches references/site-patterns/ for .md files whose domain or aliases match the input.

Usage:
  python3 match-site.py "user input text"

Output: matched site experience content, or silent if no match.
"""
import re
import sys
from pathlib import Path

PATTERNS_DIR = Path(__file__).parent.parent / 'references' / 'site-patterns'


def match_site(query: str) -> None:
    """Search site patterns and print matching content."""
    if not query or not PATTERNS_DIR.exists():
        return
    
    for entry in sorted(PATTERNS_DIR.iterdir()):
        if not entry.is_file() or entry.suffix != '.md':
            continue
        
        domain = entry.stem
        try:
            raw = entry.read_text('utf-8')
        except OSError:
            continue
        
        # Extract aliases from frontmatter
        aliases = []
        for line in raw.split('\n'):
            if line.startswith('aliases:'):
                alias_str = line.replace('aliases:', '').strip()
                alias_str = alias_str.strip('[]')
                aliases = [a.strip() for a in alias_str.split(',') if a.strip()]
                break
        
        # Build match pattern
        patterns = [re.escape(t) for t in [domain, *aliases] if t]
        if not patterns:
            continue
        regex = '|'.join(patterns)
        if not re.search(regex, query, re.IGNORECASE):
            continue
        
        # Strip frontmatter, output body
        fences = list(re.finditer(r'^---\s*$', raw, re.MULTILINE))
        if len(fences) >= 2:
            body = raw[fences[1].end():].lstrip('\n')
        else:
            body = raw
        
        print(f'--- Site Experience: {domain} ---')
        print(body.rstrip() + '\n')


if __name__ == '__main__':
    query = ' '.join(sys.argv[1:]).strip()
    if query:
        match_site(query)