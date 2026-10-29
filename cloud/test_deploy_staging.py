"""Exercise deployment staging with dummy data and a fake, local-only CLI."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Requires a POSIX bash shell')
class DeployStagingTests(unittest.TestCase):
    def test_user_state_and_dependencies_are_removed_before_source_upload(self):
        with tempfile.TemporaryDirectory(prefix='brakecheck-staging-test-') as directory:
            root = Path(directory)
            for name in ('cloud', 'backend', 'photon-agent', 'bin'):
                (root / name).mkdir()
            shutil.copyfile(Path(__file__).with_name('deploy-koyeb.sh'), root / 'cloud/deploy-koyeb.sh')
            for name in ('Dockerfile', '.dockerignore', 'backend/.env.example', 'backend/main.py',
                         'photon-agent/agent.ts', 'cloud/supervisor.mjs'):
                (root / name).write_text('fixture', encoding='utf-8')
            private = [
                'backend/.env', 'backend/.env.production', 'backend/contacts.json', 'backend/contacts.tmp',
                'backend/paired_devices.json', 'backend/paired_devices.tmp', 'backend/railway-variables.env',
                'backend/voice_cache/audio.mp3', 'backend/.venv/secret.txt', 'backend/__pycache__/data.pyc',
                'photon-agent/photon-optouts.json', 'photon-agent/photon-optouts.json.123.tmp',
                'photon-agent/photon-alerts.json', 'photon-agent/photon-alerts.json.123.tmp',
                'photon-agent/node_modules/dependency.js', 'cloud/private.log', 'cloud/device.ipa',
            ]
            for name in private:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('private dummy fixture', encoding='utf-8')
            fake = root / 'bin/koyeb'
            fake.write_text('#!/bin/sh\n[ "$1" = deploy ] || exit 1\n'
                            'find "$2" -type f -printf "%P\\n" | sort > "$STAGING_FILES"\n', encoding='utf-8')
            fake.chmod(0o700)
            listing = root / 'staged-files.txt'
            environment = {**os.environ, 'PATH': str(fake.parent) + os.pathsep + os.environ['PATH'],
                           'STAGING_FILES': str(listing)}
            result = subprocess.run(['bash', str(root / 'cloud/deploy-koyeb.sh')], input='Test Driver\n',
                                    text=True, capture_output=True, env=environment, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            staged = set(listing.read_text().splitlines())
            self.assertTrue({'backend/.env.example', 'backend/main.py', 'cloud/supervisor.mjs',
                             'photon-agent/agent.ts'}.issubset(staged))
            self.assertFalse(staged.intersection(private), staged.intersection(private))
            for name in private:
                self.assertTrue((root / name).exists(), 'Staging must preserve the original file: ' + name)


if __name__ == '__main__':
    unittest.main()
