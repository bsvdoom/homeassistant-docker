#!/usr/bin/env python3
"""Optional isolated integration check. Requires Docker daemon access and pulled images.

All services share a temporary container's loopback network, without published
host ports, privileged mode, host D-Bus or production data mounts.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def command(args, *, check=True, input=None):
    result = subprocess.run(args, capture_output=True, text=True, input=input)
    if check and result.returncode:
        raise RuntimeError(f'Command {args[0:3]} failed: {result.stderr[-1500:]}')
    return result


def docker(*args, **kwargs):
    return command(['docker', *args], **kwargs)


def wait_for(check, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise RuntimeError('Timed out waiting for a test service')


def main():
    services = json.loads(docker('compose', '-f', str(ROOT / 'compose.yml'),
                                 'config', '--no-interpolate', '--format', 'json').stdout)['services']
    IMAGES = {key: services[name]['image'] for key, name in {
        'db': 'mariadb', 'mqtt': 'mosquitto', 'nr': 'nodered', 'ha': 'homeassistant',
    }.items()}
    prefix = 'ha-release-' + uuid.uuid4().hex[:10]
    started = []
    root = Path(tempfile.mkdtemp(prefix=prefix + '-'))
    root.chmod(0o755)
    admin_password = secrets.token_hex(24)
    db_password = secrets.token_hex(24)
    mqtt_password = secrets.token_hex(24)
    try:
        (root / 'scripts').mkdir()
        (root / 'mqtt/config').mkdir(parents=True)
        for directory in ['mysql', 'nodered', 'ha', 'mqtt/data']:
            (root / directory).mkdir(parents=True)
        shutil.copy(ROOT / 'scripts/generate-mqtt-tls.sh', root / 'scripts')
        for filename in ['mosquitto.conf', 'acl']:
            shutil.copy(ROOT / 'mqtt/config' / filename, root / 'mqtt/config')
        command(['bash', str(root / 'scripts/generate-mqtt-tls.sh'), 'localhost'])
        docker('run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0',
               '--entrypoint', 'sh', '-v', f'{root}:/work', IMAGES['mqtt'], '-c',
               'chgrp 1883 /work/mqtt/config/tls/server.key; chown 1000:1000 /work/nodered')
        for index, username in enumerate(['homeassistant', 'nodered', 'sensor01']):
            flags = ['-b', '-c'] if index == 0 else ['-b']
            docker('run', '--rm', '--pull', 'never', '--network', 'none',
                   '--user', f'{os.getuid()}:{os.getgid()}', '--entrypoint', 'mosquitto_passwd',
                   '-v', f'{root / "mqtt/config"}:/mosquitto/config', IMAGES['mqtt'],
                   *flags, '/mosquitto/config/pwfile', username, mqtt_password)
        docker('run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0',
               '--entrypoint', 'sh', '-v', f'{root}:/work', IMAGES['mqtt'], '-c',
               'chgrp 1883 /work/mqtt/config/pwfile; chmod 640 /work/mqtt/config/pwfile')
        hashed = docker('run', '--rm', '--pull', 'never', '--network', 'none',
                        '--entrypoint', 'node', IMAGES['nr'], '-e',
                        'console.log(require("bcryptjs").hashSync(process.argv[1],8))',
                        admin_password).stdout.strip()
        # Use a dotenv file so the real runtime also verifies dollar-sign preservation.
        envfile = root / '.env'
        envfile.write_text(f"NODE_RED_ADMIN_USER=admin\nNODE_RED_ADMIN_PASSWORD_HASH='{hashed}'\n"
                           f'NODE_RED_CREDENTIAL_SECRET={secrets.token_hex(32)}\n')
        envfile.chmod(0o600)
        node_compose = root / 'nodered-compose.yml'
        node_compose.write_text(json.dumps({'services': {'nodered': {
            'image': IMAGES['nr'], 'container_name': prefix + '-nr',
            'network_mode': 'container:' + prefix + '-db',
            'environment': {
                'NODE_RED_ADMIN_USER': '${NODE_RED_ADMIN_USER:?required}',
                'NODE_RED_ADMIN_PASSWORD_HASH': '${NODE_RED_ADMIN_PASSWORD_HASH:?required}',
                'NODE_RED_CREDENTIAL_SECRET': '${NODE_RED_CREDENTIAL_SECRET:?required}'},
            'volumes': [f'{root / "nodered"}:/data',
                        f'{ROOT / "nodered-config/settings.js"}:/data/settings.js:ro'],
        }}}))
        docker('run', '-d', '--pull', 'never', '--name', prefix + '-db', '--network', 'none',
               '-e', 'MYSQL_ROOT_PASSWORD=' + secrets.token_hex(32),
               '-e', 'MYSQL_USER=homeassistant', '-e', 'MYSQL_DATABASE=homeassistant',
               '-e', 'MYSQL_PASSWORD=' + db_password,
               '-v', f'{root / "mysql"}:/var/lib/mysql',
               '-v', f'{ROOT / "mariadb-config/custom.cnf"}:/etc/mysql/conf.d/custom.cnf:ro', IMAGES['db'])
        started.append(prefix + '-db')
        wait_for(lambda: docker('exec', prefix + '-db', 'healthcheck.sh', '--connect',
                                '--innodb_initialized', check=False).returncode == 0)
        print('PASS MariaDB initialized and healthy', flush=True)

        def sql(query):
            return docker('exec', '-e', 'MYSQL_PWD=' + db_password, prefix + '-db',
                          'mariadb', '-h', '127.0.0.1', '-u', 'homeassistant', '-N',
                          'homeassistant', '-e', query).stdout.strip()

        assert sql('SELECT @@bind_address') == '127.0.0.1'
        sql('CREATE TABLE release_marker (value VARCHAR(32)); INSERT INTO release_marker VALUES ("persisted");')
        docker('compose', '--env-file', str(envfile), '-f', str(node_compose),
               '-p', prefix, 'up', '-d', '--pull', 'never')
        started.append(prefix + '-nr')

        def curl(*args, **kwargs):
            return docker('exec', '-i', prefix + '-nr', 'curl', '-sS', *args, **kwargs)

        wait_for(lambda: curl('--fail', 'http://127.0.0.1:1880/auth/login', check=False).returncode == 0)
        schema = json.loads(curl('http://127.0.0.1:1880/auth/login').stdout)
        assert schema['type'] == 'credentials'
        assert curl('-o', '/dev/null', '-w', '%{http_code}', 'http://127.0.0.1:1880/flows').stdout == '401'
        login = json.loads(curl('-X', 'POST', '-H', 'Content-Type: application/x-www-form-urlencoded',
                                '--data-binary', '@-', 'http://127.0.0.1:1880/auth/token',
                                input='client_id=node-red-admin&grant_type=password&scope=*&username=admin&password=' + admin_password).stdout)
        token = login['access_token']
        curl('--fail', '-X', 'POST', '-H', 'Authorization: Bearer ' + token,
             '-H', 'Content-Type: application/json', '--data-binary', '@-',
             'http://127.0.0.1:1880/flows', input=json.dumps([{'id': 'audit-tab', 'type': 'tab', 'label': 'audit'}]))
        # Node-RED loopback bind can be inspected from inside the shared network namespace.
        listening = curl('--fail', 'http://127.0.0.1:1880/flows', '-H', 'Authorization: Bearer ' + token)
        assert 'audit-tab' in listening.stdout
        print('PASS Node-RED rejects anonymous admin access and accepts authenticated login/deploy', flush=True)
        docker('run', '-d', '--pull', 'never', '--name', prefix + '-mqtt',
               '--network', 'container:' + prefix + '-db',
               '-v', f'{root / "mqtt/config"}:/mosquitto/config:ro',
               '-v', f'{root / "mqtt/data"}:/mosquitto/data', IMAGES['mqtt'])
        started.append(prefix + '-mqtt')
        common = ['-h', 'localhost', '-p', '8883', '--cafile', '/mosquitto/config/tls/ca.crt',
                  '-V', 'mqttv5', '-q', '1']

        def pub(topic, *, user='sensor01', password=mqtt_password):
            auth = ['-u', user, '-P', password] if user else []
            return docker('exec', prefix + '-mqtt', 'mosquitto_pub', *common, *auth,
                          '-t', topic, '-m', 'persisted', '-r', check=False)

        wait_for(lambda: pub('devices/sensor01/test').returncode == 0)
        assert pub('devices/sensor01/test', user=None).returncode != 0
        assert pub('devices/sensor01/test', password='incorrect').returncode != 0
        denied = pub('devices/sensor02/test')
        assert denied.returncode != 0 or 'not authorized' in (denied.stdout + denied.stderr).lower(), denied.stdout + denied.stderr
        assert pub('homeassistant/sensor/sensor01/test/config').returncode == 0
        denied = pub('homeassistant/sensor/sensor02/test/config')
        assert denied.returncode != 0 or 'not authorized' in (denied.stdout + denied.stderr).lower(), denied.stdout + denied.stderr

        def retained():
            return docker('exec', prefix + '-mqtt', 'mosquitto_sub', *common, '-u', 'sensor01',
                          '-P', mqtt_password, '-t', 'devices/sensor01/test', '-C', '1', '-W', '5').stdout.strip()

        assert retained() == 'persisted'
        print('PASS MQTT TLS, password rejection, publish/subscribe and per-device ACL', flush=True)
        (root / 'ha/recorder.yaml').write_text(
            f'db_url: mysql://homeassistant:{db_password}@127.0.0.1/homeassistant?charset=utf8mb4\n')
        (root / 'ha/configuration.yaml').write_text(
            'http:\nhistory:\nrecorder: !include recorder.yaml\n'
            'template:\n  - sensor:\n      - name: Release audit sensor\n        state: "22"\n')
        docker('run', '-d', '--pull', 'never', '--name', prefix + '-ha',
               '--network', 'container:' + prefix + '-db', '-e', 'TZ=Europe/Budapest',
               '-v', f'{root / "ha"}:/config', IMAGES['ha'])
        started.append(prefix + '-ha')
        wait_for(lambda: 'states' in sql('SHOW TABLES'), seconds=180)
        wait_for(lambda: int(sql('SELECT COUNT(*) FROM states')) > 0, seconds=120)
        print('PASS Home Assistant recorder created schema and wrote state data to MariaDB', flush=True)
        docker('restart', prefix + '-nr', prefix + '-mqtt')
        wait_for(lambda: curl('--fail', 'http://127.0.0.1:1880/auth/login', check=False).returncode == 0)
        assert 'audit-tab' in (root / 'nodered/flows.json').read_text()
        wait_for(lambda: pub('devices/sensor01/ready').returncode == 0)
        assert retained() == 'persisted'
        assert sql('SELECT value FROM release_marker') == 'persisted'
        print('PASS restart preserves Node-RED flow and MQTT retained state', flush=True)
        # Exercise the documented cold-file backup and restore on disposable data.
        for name in reversed(started):
            docker('stop', name)
        docker('run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0',
               '--entrypoint', 'sh', '-v', f'{root}:/work', IMAGES['mqtt'], '-c',
               'set -e; tar -czf /work/backup.tar.gz -C /work mysql nodered ha mqtt; '
               'rm -rf /work/mysql /work/nodered /work/ha /work/mqtt; '
               'tar -xzf /work/backup.tar.gz -C /work; '
               'chown ' + str(os.getuid()) + ':' + str(os.getgid()) + ' /work/backup.tar.gz; '
               'chmod 600 /work/backup.tar.gz')
        docker('start', prefix + '-db')
        wait_for(lambda: docker('exec', prefix + '-db', 'healthcheck.sh', '--connect',
                                '--innodb_initialized', check=False).returncode == 0)
        docker('start', prefix + '-nr', prefix + '-mqtt', prefix + '-ha')
        wait_for(lambda: curl('--fail', 'http://127.0.0.1:1880/auth/login', check=False).returncode == 0)
        wait_for(lambda: pub('devices/sensor01/ready').returncode == 0)
        assert retained() == 'persisted'
        assert 'audit-tab' in (root / 'nodered/flows.json').read_text()
        assert sql('SELECT value FROM release_marker') == 'persisted'
        assert int(sql('SELECT COUNT(*) FROM states')) > 0
        print('PASS cold backup/restore preserves database, flows, TLS credentials and retained messages', flush=True)
    finally:
        for name in reversed(started):
            docker('rm', '-f', name, check=False)
        # Only remove data created in this randomly named temporary test directory.
        docker('run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0',
               '--entrypoint', 'sh', '-v', f'{root}:/work', IMAGES['mqtt'],
               '-c', 'rm -rf /work/mysql /work/nodered /work/ha /work/mqtt/data', check=False)
        shutil.rmtree(root)


if __name__ == '__main__':
    main()
