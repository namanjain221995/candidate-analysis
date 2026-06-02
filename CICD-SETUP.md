# CI/CD Setup — GitHub Actions → EC2

On every push to `main`, GitHub Actions runs checks, then SSHes into the EC2,
pulls the latest code, reinstalls dependencies, and restarts both services.

```
push to main → CI (compile + lint + import test) → CD (ssh: git pull, pip install, restart services)
```

## Files
- `.github/workflows/ci-cd.yml` — the workflow (commit this to the repo)
- `cicd_ec2_setup.sh`           — run ONCE on the EC2 (passwordless service restart)

## One-time setup

### 1. On the EC2 — allow passwordless service restart
GitHub Actions restarts the services via `sudo systemctl restart`. Without this,
sudo would prompt for a password and the deploy hangs.
```bash
cd /home/ec2-user/candidate-analysis
bash cicd_ec2_setup.sh
```

### 2. Create an SSH key for GitHub → EC2
On your laptop (or the EC2), make a dedicated deploy key:
```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-deploy"
```
This creates `deploy_key` (private) and `deploy_key.pub` (public).

Add the PUBLIC key to the EC2's authorized_keys:
```bash
# copy deploy_key.pub contents, then on the EC2:
echo "PASTE_deploy_key.pub_CONTENTS" >> ~/.ssh/authorized_keys
```

### 3. Add GitHub repository secrets
GitHub repo → Settings → Secrets and variables → Actions → New repository secret.
Add these three:

| Secret name   | Value                                            |
|---------------|--------------------------------------------------|
| `EC2_HOST`    | the EC2 public IP or DNS (e.g. `13.x.x.x`)       |
| `EC2_USER`    | `ec2-user`                                       |
| `EC2_SSH_KEY` | the FULL contents of the PRIVATE `deploy_key`    |

For `EC2_SSH_KEY`, paste everything including the
`-----BEGIN OPENSSH PRIVATE KEY-----` / `-----END ...-----` lines.

### 4. Make sure the EC2 security group allows SSH
The EC2's security group must allow inbound SSH (port 22). GitHub Actions runners
use changing IPs, so either:
- allow 22 from 0.0.0.0/0 (simplest, but open — fine for a key-only login), or
- restrict to GitHub's published Actions IP ranges (tighter, more maintenance).

### 5. Commit the workflow
```bash
git add .github/workflows/ci-cd.yml cicd_ec2_setup.sh CICD-SETUP.md
git commit -m "add CI/CD pipeline"
git push origin main
```
That push will itself trigger the first run — watch it under the repo's "Actions" tab.

## How to verify
- Repo → Actions tab → you'll see the `CI-CD` run
- CI job: green check = code compiles + imports
- Deploy job: green check = EC2 pulled and restarted
- On the EC2: `journalctl -u llm-service -n 20` shows the restart

## Notes
- The deploy only runs on push to `main` (not on PRs — PRs only run CI).
- If a service fails to restart, the workflow step fails and you'll see it in Actions.
- `git pull` on the EC2 must be clean — don't edit files directly on the EC2, or
  the pull will conflict. Treat the repo as the source of truth.