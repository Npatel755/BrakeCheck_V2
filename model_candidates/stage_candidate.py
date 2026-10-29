"""Prepare a separate comma.ai/Intel resource folder without changing the app bundle."""
import argparse
import json
from pathlib import Path
import shutil

from update_bundle_manifest import write_manifest


ROOT = Path(__file__).resolve().parent


def stage(output: Path, source: Path = ROOT):
    # Refuse existing output so a trial cannot overwrite a user's model bundle.
    if output.exists():
        raise FileExistsError(f'Choose a new output folder: {output}')
    for name in ('CommaLane', 'IntelRoad'):
        package = source / f'{name}.mlpackage'
        manifest = json.loads((package / 'Manifest.json').read_text(encoding='utf-8'))
        for item in manifest['itemInfoEntries'].values():
            asset = package / 'Data' / item['path']
            if not asset.exists():
                raise FileNotFoundError(asset)
        weights = package / 'Data/com.apple.CoreML/weights/weight.bin'
        if not weights.is_file() or not weights.stat().st_size:
            raise FileNotFoundError(weights)
    output.mkdir(parents=True)
    for name in ('CommaLane', 'IntelRoad'):
        shutil.copytree(source / f'{name}.mlpackage', output / f'{name}.mlpackage')
    write_manifest(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(stage(args.output))


if __name__ == '__main__':
    main()
