"""Release checks run without starting services or using real credentials."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Deliberately artificial; runtime authentication tests generate a real bcrypt hash.
DUMMY_HASH = '$2b$08$' + 'a' * 53
VALUES = {
    'MYSQL_ROOT_PASSWORD': 'audit-dummy-root',
    'MYSQL_DATABASE': 'homeassistant',
    'MYSQL_USER': 'homeassistant',
    'MYSQL_PASSWORD': 'audit-dummy-user',
    'NODE_RED_ADMIN_USER': 'admin',
    'NODE_RED_ADMIN_PASSWORD_HASH': DUMMY_HASH,
    'NODE_RED_CREDENTIAL_SECRET': 'audit-dummy-key-' + 'x' * 32,
}


def clean_env():
    env = os.environ.copy()
    for name in VALUES:
        env.pop(name, None)
    return env


class ComposeTests(unittest.TestCase):
    def compose(self, env, *args):
        return subprocess.run(
            ['docker', 'compose', '--env-file', '/dev/null', *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    def test_resolved_config(self):
        result = self.compose(clean_env() | VALUES, 'config', '--format', 'json')
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        self.assertEqual(set(services), {'mariadb', 'homeassistant', 'mosquitto', 'nodered'})
        self.assertEqual(services['homeassistant']['depends_on']['mariadb']['condition'], 'service_healthy')
        self.assertIn('--innodb_initialized', services['mariadb']['healthcheck']['test'])
        self.assertEqual(services['homeassistant']['environment']['TZ'], 'Europe/Budapest')
        for service in services.values():
            self.assertNotIn('@sha256:', service['image'])
            self.assertIn(service['image'].rsplit(':', 1)[1], {'latest', 'stable'})
            self.assertEqual(service['logging']['driver'], 'none')
            self.assertNotIn('options', service['logging'])
        settings = next(v for v in services['nodered']['volumes'] if v['target'] == '/data/settings.js')
        self.assertTrue(settings['read_only'])
        self.assertFalse(settings['bind']['create_host_path'])

    def test_missing_and_empty_secrets_rejected(self):
        for key in VALUES:
            for value in [None, '']:
                with self.subTest(key=key, value=value):
                    env = clean_env() | VALUES
                    if value is None:
                        env.pop(key)
                    else:
                        env[key] = value
                    self.assertNotEqual(self.compose(env, 'config', '--quiet').returncode, 0)

    def test_dotenv_bcrypt_roundtrip(self):
        with tempfile.NamedTemporaryFile('w') as envfile:
            for key, value in VALUES.items():
                envfile.write(f"{key}='{value}'\n")
            envfile.flush()
            result = subprocess.run(
                ['docker', 'compose', '--env-file', envfile.name, 'config', '--environment'],
                cwd=ROOT, env=clean_env(), capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            value = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)['NODE_RED_ADMIN_PASSWORD_HASH']
            self.assertEqual(value, DUMMY_HASH)


class NodeRedTests(unittest.TestCase):
    def settings(self, env):
        return subprocess.run(
            ['node', '-e', 'console.log(JSON.stringify(require(process.argv[1])))',
             str(ROOT / 'nodered-config/settings.js')],
            env=env, capture_output=True, text=True,
        )

    def test_loopback_and_authentication(self):
        result = self.settings(clean_env() | VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        self.assertEqual(config['uiHost'], '127.0.0.1')
        self.assertNotIn('default', config['adminAuth'])
        self.assertEqual(config['adminAuth']['users'][0]['password'], DUMMY_HASH)
        self.assertEqual(config['httpNodeAuth']['pass'], DUMMY_HASH)
        self.assertEqual(config['httpStaticAuth']['pass'], DUMMY_HASH)

    def test_invalid_or_missing_credentials_fail_closed(self):
        for key, value in [('NODE_RED_ADMIN_PASSWORD_HASH', None),
                           ('NODE_RED_ADMIN_PASSWORD_HASH', 'plaintext'),
                           ('NODE_RED_ADMIN_PASSWORD_HASH', '$2b$99$' + 'a' * 53),
                           ('NODE_RED_ADMIN_USER', ' '),
                           ('NODE_RED_CREDENTIAL_SECRET', 'short')]:
            with self.subTest(key=key, value=value):
                env = clean_env() | VALUES
                if value is None:
                    env.pop(key)
                else:
                    env[key] = value
                self.assertNotEqual(self.settings(env).returncode, 0)


class RecorderTests(unittest.TestCase):
    def test_url_special_characters(self):
        spec = importlib.util.spec_from_file_location('recorder', ROOT / 'scripts/configure-recorder.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = {'MYSQL_USER': 'ha@local', 'MYSQL_PASSWORD': 'a:@/?#%$ és', 'MYSQL_DATABASE': 'ha/data'}
        text = module.recorder_config(values)
        url = json.loads(text.splitlines()[0].split(': ', 1)[1])
        parsed = urlsplit(url)
        self.assertEqual(unquote(parsed.username), values['MYSQL_USER'])
        self.assertEqual(unquote(parsed.password), values['MYSQL_PASSWORD'])
        self.assertEqual(unquote(parsed.path[1:]), values['MYSQL_DATABASE'])
        self.assertEqual(parsed.hostname, '127.0.0.1')

    def test_private_file_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'bin').mkdir()
            shutil.copy(ROOT / 'scripts/configure-recorder.py', root / 'scripts')
            docker = root / 'bin/docker'
            values = VALUES | {'MYSQL_PASSWORD': 'test-$dollar:@/#%='}
            docker.write_text('#!/usr/bin/env python3\nprint(' +
                              repr('\n'.join(f'{k}={v}' for k, v in values.items())) + ')\n')
            docker.chmod(0o755)
            env = os.environ | {'PATH': str(root / 'bin') + ':' + os.environ['PATH']}
            args = ['python3', str(root / 'scripts/configure-recorder.py')]
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = root / 'homeassistant/config/recorder.yaml'
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            content = target.read_bytes()
            url = json.loads(content.decode().splitlines()[0].split(': ', 1)[1])
            self.assertEqual(unquote(urlsplit(url).password), values['MYSQL_PASSWORD'])
            result = subprocess.run(args, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), content)
            self.assertNotIn(values['MYSQL_PASSWORD'], result.stdout + result.stderr)


class TLSTests(unittest.TestCase):
    def test_certificate_identity_and_preservation(self):
        for identity in ['mqtt.example.lan', '192.0.2.10']:
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'scripts').mkdir()
                (root / 'mqtt/config').mkdir(parents=True)
                shutil.copy(ROOT / 'scripts/generate-mqtt-tls.sh', root / 'scripts')
                args = ['bash', str(root / 'scripts/generate-mqtt-tls.sh'), identity]
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                tls = root / 'mqtt/config/tls'
                check = '-verify_ip' if identity[0].isdigit() else '-verify_hostname'
                result = subprocess.run(['openssl', 'verify', '-CAfile', str(tls / 'ca.crt'),
                                         check, identity, str(tls / 'server.crt')], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((tls / 'ca.key').stat().st_mode & 0o777, 0o600)
                self.assertEqual((tls / 'server.key').stat().st_mode & 0o777, 0o640)
                cert = (tls / 'server.crt').read_bytes()
                self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
                self.assertEqual((tls / 'server.crt').read_bytes(), cert)


if __name__ == '__main__':
    unittest.main()
