#!/usr/bin/env bash
# One-shot EC2 setup for the transcript service (Amazon Linux 2023).
# Run from inside the cloned repo folder:  bash setup_ec2.sh
set -euo pipefail

echo "==> Installing system packages"
sudo dnf install -y python3 python3-pip wget tar xz

echo "==> Installing ffmpeg (static build)"
if ! command -v ffmpeg >/dev/null 2>&1; then
  cd /tmp
  wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar xf ffmpeg-release-amd64-static.tar.xz
  sudo cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/
  rm -rf ffmpeg-*-static ffmpeg-release-amd64-static.tar.xz
  cd - >/dev/null
fi
ffmpeg -version | head -1

echo "==> Creating Python virtual environment"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "==> Done. Next:"
echo "   1. cp .env.example .env   then edit .env with your real values"
echo "   2. source venv/bin/activate && python main.py   (to test)"
echo "   3. To run as a service, see README.md (systemd section)"
