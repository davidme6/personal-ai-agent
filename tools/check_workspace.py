#!/usr/bin/env python3
"""Validate the private workspace structure and reject unsafe registry paths."""
import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def inside(root, value):
    relative = Path(value)
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root):
        raise ValueError(f'Path escapes the workspace: {value}')
    return target


def check(root):
    root = root.resolve()
    required = ['AGENTS.md', '.personal/profile.md', '.personal/current-state.md',
                '.personal/project-registry.json', '.device/device.json']
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError('Missing files: ' + ', '.join(missing))
    registry = json.loads((root / '.personal/project-registry.json').read_text(encoding='utf-8'))
    projects = registry.get('projects')
    if not isinstance(projects, list) or not projects:
        raise ValueError('The project registry must contain a non-empty projects list.')
    for project in projects:
        for key in ('id', 'entry', 'state'):
            if not project.get(key):
                raise ValueError(f'Project is missing {key}.')
        for key in ('entry', 'state'):
            target = inside(root, project[key])
            if not target.is_file():
                raise ValueError(f"Missing {key} for {project['id']}: {project[key]}")
    return len(projects)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        count = check(args.root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Workspace check failed: {exc}\n')
    print(f'Workspace check passed: {count} project(s), root {args.root.resolve()}')


if __name__ == '__main__':
    main()
