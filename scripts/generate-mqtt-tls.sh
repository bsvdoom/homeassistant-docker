#!/usr/bin/env bash
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."
if (( $# != 1 )); then
    echo "Usage: bash scripts/generate-mqtt-tls.sh <broker DNS name or IPv4 address>" >&2
    exit 1
fi
broker="$1"
if [[ ! "$broker" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    echo "Use a DNS name or IPv4 address (no scheme, port or whitespace)." >&2
    exit 1
fi
target="mqtt/config/tls"
if [[ -e "$target" ]]; then
    echo "$target already exists; preserve it or move it aside before certificate renewal." >&2
    exit 1
fi
work="$(mktemp -d mqtt/config/.tls-XXXXXX)"
trap 'rm -rf "$work"' EXIT
san="DNS:localhost,IP:127.0.0.1"
if [[ "$broker" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    san+=",IP:$broker"
else
    san+=",DNS:$broker"
fi
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -subj "/CN=Home MQTT CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$work/ca.key" -out "$work/ca.crt"
openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -subj "/CN=$broker" -keyout "$work/server.key" -out "$work/server.csr"
cat > "$work/server.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$san
EOF
openssl x509 -req -sha256 -days 365 -in "$work/server.csr" \
    -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial \
    -extfile "$work/server.ext" -out "$work/server.crt"
openssl verify -CAfile "$work/ca.crt" "$work/server.crt"
rm "$work/server.csr" "$work/server.ext" "$work/ca.srl"
chmod 755 "$work"
chmod 644 "$work/ca.crt" "$work/server.crt"
# The broker runs as UID/GID 1883; its private key is not world-readable.
chmod 640 "$work/server.key"
mv "$work" "$target"
echo "Created $target. Set server.key group to 1883 before starting the broker."
