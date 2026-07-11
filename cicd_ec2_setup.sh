#!/usr/bin/env bash
# One-time setup on the EC2 so GitHub Actions can restart the services
# without an interactive sudo password prompt.
#
# Run ONCE on the EC2:  bash cicd_ec2_setup.sh
set -euo pipefail

echo "==> Allowing ec2-user to restart the three services without a password"
SUDO_FILE=/etc/sudoers.d/candidate-analysis-deploy
sudo tee "$SUDO_FILE" >/dev/null <<'EOF'
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart transcript-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart llm-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart code-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active transcript-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active llm-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active code-service
EOF
sudo chmod 440 "$SUDO_FILE"
sudo visudo -c -f "$SUDO_FILE"

echo "==> Making sure git pull works non-interactively"
cd /home/ec2-user/candidate-analysis
git config --global --add safe.directory /home/ec2-user/candidate-analysis || true

echo "==> Done. GitHub Actions can now pull and restart services."