#!/usr/bin/env bash
set -euo pipefail

SSH_PASS="${HA_SSH_PASSWORD:-ha_test_pass}"
KEY_PASSPHRASE="${HA_SSH_PASSPHRASE:-test-key-passphrase}"
KEY_DIR="${HA_SSH_KEY_DIR:-/ssh-keys}"

mkdir -p /run/sshd /config/custom_components /root/.ssh "${KEY_DIR}"
chmod 700 /root/.ssh

echo "root:${SSH_PASS}" | chpasswd

# Generate SSH host keys if not present
ssh-keygen -A

# Generate test client SSH keys (unencrypted + passphrase-encrypted) in shared volume
if [ ! -f "${KEY_DIR}/id_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -f "${KEY_DIR}/id_ed25519" -C "ha-ccm-test-unencrypted" >/dev/null
fi
if [ ! -f "${KEY_DIR}/id_ed25519_encrypted" ]; then
    ssh-keygen -t ed25519 -N "${KEY_PASSPHRASE}" -f "${KEY_DIR}/id_ed25519_encrypted" -C "ha-ccm-test-encrypted" >/dev/null
fi

chmod 600 "${KEY_DIR}/id_ed25519" "${KEY_DIR}/id_ed25519_encrypted"
chmod 644 "${KEY_DIR}/id_ed25519.pub" "${KEY_DIR}/id_ed25519_encrypted.pub"

cat "${KEY_DIR}/id_ed25519.pub" "${KEY_DIR}/id_ed25519_encrypted.pub" > /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# Configure sshd for password and public key authentication
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

/usr/sbin/sshd

exec python3 -m docker.mock_ha_api
