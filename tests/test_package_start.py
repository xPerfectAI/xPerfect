"""Critical-path checks for the public startup command; no Docker or provider calls."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('package_start', ROOT / 'deployment/linux/launch.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class PackageStartTests(unittest.TestCase):
    def run_start(self, arguments, callback):
        with patch.object(sys, 'argv', ['launch.py', *arguments]), \
             patch.object(launch, 'local_docker_endpoint', return_value='unix:///test.sock'), \
             patch.object(launch, 'host_route_overlaps', return_value={}), \
             patch.object(launch, 'launch', side_effect=callback), \
             contextlib.redirect_stdout(io.StringIO()):
            launch.main()

    def test_public_command_reaches_the_existing_launcher_without_host_dependencies(self):
        result = subprocess.run([sys.executable, str(ROOT / 'xperfect'), 'start', '--docker', '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--state-dir', result.stdout)
        self.assertIn('--native-image', result.stdout)

    def test_host_start_command_keeps_its_own_help(self):
        result = subprocess.run([sys.executable, str(ROOT / 'xperfect'), 'start', '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--native-image', result.stdout)

    def test_manifest_defaults_create_private_outputs_and_keep_the_requested_model(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve() / 'private'
            seen = {}

            def started(**arguments):
                seen.update(arguments)
                arguments['credentials'].write('{"synthetic": "test-only"}')
                return {'ui_url': 'http://127.0.0.1:8780'}

            self.run_start(['--state-dir', str(state), '--model', 'codex-cli=chosen-model'], started)
            release, defaults = launch.published_release()
            self.assertEqual(seen['image'], release['xperfect-service'])
            self.assertEqual(seen['native_image'], release['xperfect-native'])
            self.assertEqual(seen['models'], {**defaults, 'codex-cli': 'chosen-model'})
            self.assertEqual(seen['name'], 'xperfect-local')
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            for name in ('receipt.json', 'credentials.json'):
                self.assertEqual((state / name).stat().st_mode & 0o777, 0o600)

    def test_explicit_images_and_outputs_do_not_depend_on_release_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            arguments = ['--name', 'xperfect-other', '--service-image', 'sha256:' + 'a' * 64,
                         '--native-image', 'sha256:' + 'b' * 64,
                         '--receipt', str(state / 'operator-receipt.json'),
                         '--credentials', str(state / 'operator-credentials.json')]
            seen = {}

            def started(**values):
                seen.update(values)
                values['credentials'].write('{}')
                return {'ui_url': 'http://127.0.0.1:8780'}

            with patch.object(launch, 'published_release', side_effect=AssertionError('unneeded metadata')):
                self.run_start(arguments, started)
            self.assertEqual(seen['image'], 'sha256:' + 'a' * 64)
            self.assertEqual(seen['native_image'], 'sha256:' + 'b' * 64)
            self.assertEqual(seen['models'], {})

    def test_public_start_carries_reviewed_models_without_an_implicit_runtime_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            seen = {}

            def started(**values):
                seen.update(values)
                values['credentials'].write('{}')
                return {'ui_url': 'http://127.0.0.1:8780'}

            self.run_start(['--state-dir', str(Path(folder).resolve())], started)
            self.assertEqual(seen['models'], launch.published_release()[1])
            self.assertTrue(seen['models']['codex-cli'])

    def test_existing_credentials_are_never_replaced_or_used_to_start_another_package(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            private = state / 'credentials.json'
            private.write_bytes(b'prior synthetic state')
            with self.assertRaises(FileExistsError):
                self.run_start(['--state-dir', str(state)], lambda **kw: self.fail('must not launch'))
            self.assertEqual(private.read_bytes(), b'prior synthetic state')

    def test_symlink_state_is_refused_before_any_package_output_is_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / 'other'
            target.mkdir()
            (root / 'link').symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'real directory'):
                self.run_start(['--state-dir', str(root / 'link')], lambda **kw: self.fail('must not launch'))
            self.assertEqual(list(target.iterdir()), [])

    def test_invalid_manifest_fails_before_docker_or_state_mutation(self):
        with patch.object(Path, 'read_text', return_value=json.dumps({'registry': 'ghcr.io/example', 'images': []})):
            with self.assertRaisesRegex(ValueError, 'release metadata'):
                self.run_start([], lambda **kw: self.fail('must not launch'))

    def test_a_package_name_cannot_escape_its_private_state_location(self):
        with self.assertRaisesRegex(ValueError, 'Package name'):
            self.run_start(['--name', '../other'], lambda **kw: self.fail('must not launch'))


if __name__ == '__main__':
    unittest.main()
