"""Build a standalone Flowvy MOTD release and its SHA-256 checksum."""

import argparse
import hashlib
from pathlib import Path
import shutil
import tempfile
import zipapp

SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'flowvy_motd'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, nargs='?', default=Path('dist'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    bundle = args.output / 'flowvy-motd.pyz'
    with tempfile.TemporaryDirectory(prefix='flowvy-build-') as temporary:
        root = Path(temporary)
        shutil.copytree(SOURCE, root / 'flowvy_motd',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        zipapp.create_archive(root, target=bundle, interpreter='/usr/bin/python3',
                              main='flowvy_motd.cli:entrypoint', compressed=True)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.with_suffix('.pyz.sha256').write_text(f'{digest}  {bundle.name}\n')
    print(bundle)


if __name__ == '__main__':
    main()
