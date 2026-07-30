#!/usr/bin/env bash
# Create the only TLS material accepted by the local operator launcher.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required; install it with: brew install mkcert" >&2
  exit 1
fi

mkdir -p .local/tls
chmod 700 .local .local/tls
mkcert -install
mkcert_ca_root="$(mkcert -CAROOT)"
install -m 0644 "$mkcert_ca_root/rootCA.pem" .local/tls/rootCA.pem
mkcert -cert-file .local/tls/localhost.pem \
  -key-file .local/tls/localhost-key.pem \
  localhost 127.0.0.1 ::1
chmod 0644 .local/tls/localhost.pem
chmod 0644 .local/tls/rootCA.pem
chmod 0600 .local/tls/localhost-key.pem
uv run python -m trading_assistant.ops.tls inspect
