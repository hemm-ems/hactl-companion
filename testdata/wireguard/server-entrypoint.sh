#!/bin/bash
set -euo pipefail

echo "[wg-server] Generating keypairs..."
wg genkey | tee /tmp/server.key | wg pubkey > /tmp/server.pub
wg genkey | tee /tmp/client.key | wg pubkey > /tmp/client.pub

SERVER_KEY=$(cat /tmp/server.key)
SERVER_PUB=$(cat /tmp/server.pub)
CLIENT_KEY=$(cat /tmp/client.key)
CLIENT_PUB=$(cat /tmp/client.pub)

echo "[wg-server] Writing server config..."
mkdir -p /etc/wireguard
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.13.13.1/24
ListenPort = 51820
PrivateKey = ${SERVER_KEY}

[Peer]
PublicKey = ${CLIENT_PUB}
AllowedIPs = 10.13.13.2/32
EOF
chmod 600 /etc/wireguard/wg0.conf

echo "[wg-server] Writing client config to /shared/..."
mkdir -p /shared
cat > /shared/client.conf <<EOF
[Interface]
PrivateKey = ${CLIENT_KEY}
Address = 10.13.13.2/24

[Peer]
PublicKey = ${SERVER_PUB}
Endpoint = wg-server:51820
AllowedIPs = 10.13.13.0/24
PersistentKeepalive = 25
EOF

# Also write as JSON for testing the JSON endpoint
cat > /shared/client.json <<EOF
{
  "tunnel_name": "wg0",
  "interface": {
    "private_key": "${CLIENT_KEY}",
    "address": "10.13.13.2/24"
  },
  "peers": [
    {
      "public_key": "${SERVER_PUB}",
      "endpoint": "wg-server:51820",
      "allowed_ips": "10.13.13.0/24",
      "persistent_keepalive": 25
    }
  ]
}
EOF

echo "[wg-server] Starting WireGuard interface..."
wg-quick up wg0

echo "[wg-server] Interface up. Marking ready."
touch /shared/ready

echo "[wg-server] Ready. Keeping container alive..."
exec tail -f /dev/null
