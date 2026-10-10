#!/usr/bin/env python3
"""Copy retrieval tests/dependencies to scratch; never build/publish the wiki.

Usage: python3 _tools/run_stage2_retrieval_tests.py --live-root /home/hermes/wiki
       --staged-root /home/hermes/wiki/.ingestion-rollout/2026-10-10
The nested home/wiki layout preserves existing designated-scratch validation.
Only node_modules is symlinked. All source modules are private copies.
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-root', type=Path, default=Path('/home/hermes/wiki'))
    parser.add_argument('--staged-root', type=Path, default=Path('/home/hermes/wiki/.ingestion-rollout/2026-10-10'))
    args = parser.parse_args()
    repo, staged = args.live_root.resolve(), args.staged_root.resolve()
    base = Path('/home/hermes/.hermes/cache/scratch') / ('stage2-retrieval-run-' + str(time.time_ns()))
    root = base / 'home/hermes/wiki'
    root.mkdir(parents=True)
    (root.parent / '.hermes/cache/scratch').mkdir(parents=True)
    shutil.copytree(repo / '.vitepress/semantic', root / '.vitepress/semantic')
    shutil.copytree(repo / '.vitepress/semantic', root / '.vitepress/legacy-semantic')
    (root / 'node_modules').symlink_to(repo / 'node_modules', target_is_directory=True)
    paths = ['.vitepress/semantic/corpus.mjs', '.vitepress/semantic/passages.mjs', '.vitepress/semantic-query.mjs',
             '_tools/wiki_ops.py', '.vitepress/test-stage2-retrieval.mjs', '_tools/test_stage2_retrieval_cli.py']
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged / path, root / path)
    results = []
    commands = [['node', str(root / '.vitepress/test-stage2-retrieval.mjs')],
                ['python3', str(root / '_tools/test_stage2_retrieval_cli.py')]]
    for cmd in commands:
        result = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        data = dict(command=cmd, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        results.append(data)
        print(json.dumps(data, indent=2))
    (base / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print('TEST_COPY_ROOT=' + str(root))
    print('RESULTS=' + str(base / 'results.json'))
    return 0 if all(result['exit_code'] == 0 for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
