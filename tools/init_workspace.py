#!/usr/bin/env python3
"""Create a private runtime beside the public framework without overwriting user files."""
import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def copy_new(source, target):
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def initialize(root):
    root = root.resolve()
    if not (root / 'AGENTS.md').is_file():
        raise ValueError('AGENTS.md not found; run this tool from a valid framework copy.')
    personal = root / '.personal'
    device = root / '.device'
    for path in (personal / 'projects/example-project/logs', personal / 'knowledge',
                 personal / 'logs/conversations', device / 'secrets'):
        path.mkdir(parents=True, exist_ok=True)
    created = []
    pairs = [
        ('templates/profile.md', '.personal/profile.md'),
        ('templates/current-state.md', '.personal/current-state.md'),
        ('config/project-registry.example.json', '.personal/project-registry.json'),
        ('templates/project-entry.md', '.personal/projects/example-project/ENTRY.md'),
        ('templates/project-state.md', '.personal/projects/example-project/STATE.md'),
    ]
    for source, target in pairs:
        if copy_new(root / source, root / target):
            created.append(target)
    device_file = device / 'device.json'
    if not device_file.exists():
        device_file.write_text(json.dumps({
            'version': 1,
            'workspace_root': str(root),
            'note': 'Machine-local file. Do not sync or commit.'
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        created.append('.device/device.json')
    return created


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        created = initialize(args.root)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Initialization failed: {exc}\n')
    print(f'Workspace ready. Created {len(created)} missing files; existing files were preserved.')


if __name__ == '__main__':
    main()
