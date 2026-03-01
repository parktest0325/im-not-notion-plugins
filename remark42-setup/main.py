#!/usr/bin/env python3
"""Install and manage Remark42 self-hosted comment server."""

import json
import os
import subprocess
import sys
import secrets
import re
import time

REMARK42_DIR = os.path.expanduser("~/.remark42")
REMARK42_BIN = os.path.join(REMARK42_DIR, "remark42")
REMARK42_VAR = os.path.join(REMARK42_DIR, "var")
REMARK42_LOG = os.path.join(REMARK42_DIR, "remark42.log")
REMARK42_ENV = os.path.join(REMARK42_DIR, "env.json")
REMARK42_AUTO_BACKUP = os.path.join(REMARK42_DIR, "backups")
GITHUB_RELEASE_URL = "https://github.com/umputun/remark42/releases/latest/download"


def load_server_config():
    config_path = os.path.expanduser("~/.inn_server_config.json")
    with open(config_path, "r") as f:
        return json.load(f)


def get_base_path(data):
    ctx = data.get("context", {})
    base_path = ctx.get("base_path", "")
    if base_path:
        return base_path
    try:
        config = load_server_config()
        return config.get("cms_config", {}).get("hugo_config", {}).get("base_path", "")
    except Exception:
        return ""


def get_site_url():
    """Get site URL from server config (base_url)."""
    try:
        config = load_server_config()
        url = config.get("cms_config", {}).get("hugo_config", {}).get("url", "")
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return ""


def run(cmd, check=False):
    """Run shell command, return (returncode, stdout)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{r.stderr}")
    return r.returncode, r.stdout.strip()


def detect_arch():
    """Detect server architecture for download."""
    _, arch = run("uname -m")
    mapping = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "arm",
    }
    return mapping.get(arch, arch)


def detect_os():
    """Detect OS for download."""
    _, osname = run("uname -s")
    return osname.lower()  # linux, darwin


def download_binary():
    """Download Remark42 binary from GitHub releases."""
    arch = detect_arch()
    osname = detect_os()
    filename = f"remark42.{osname}-{arch}.tar.gz"
    url = f"{GITHUB_RELEASE_URL}/{filename}"
    tmp = f"/tmp/{filename}"

    os.makedirs(REMARK42_DIR, exist_ok=True)

    # Download
    rc, _ = run(f'curl -fSL -o "{tmp}" "{url}"')
    if rc != 0:
        raise RuntimeError(f"Failed to download Remark42 from {url}")

    # Extract — remark42 binary is inside the tarball
    rc, _ = run(f'tar -xzf "{tmp}" -C "{REMARK42_DIR}"')
    if rc != 0:
        raise RuntimeError(f"Failed to extract {tmp}")

    # Rename platform-specific binary to generic name
    extracted = os.path.join(REMARK42_DIR, f"remark42.{osname}-{arch}")
    if os.path.isfile(extracted) and not os.path.isfile(REMARK42_BIN):
        os.rename(extracted, REMARK42_BIN)

    # Ensure executable
    run(f'chmod +x "{REMARK42_BIN}"')

    # Cleanup
    os.remove(tmp)

    # Verify
    rc, version = run(f'"{REMARK42_BIN}" --version 2>&1 || true')
    return version or "installed"


def save_env(env_data):
    """Save environment config for restart."""
    with open(REMARK42_ENV, "w") as f:
        json.dump(env_data, f, indent=2)


def load_env():
    """Load saved environment config."""
    if not os.path.isfile(REMARK42_ENV):
        return None
    with open(REMARK42_ENV, "r") as f:
        return json.load(f)


def patch_hugo_toml(base_path, site_url, port, site_id="blog", locale="ko"):
    """Add [params.remark42] section to hugo.toml if not present."""
    toml_path = os.path.join(base_path, "hugo.toml")
    if not os.path.isfile(toml_path):
        return "hugo.toml not found — skipped"

    with open(toml_path, "r") as f:
        content = f.read()

    # Check if remark42 section already exists
    if "[params.remark42]" in content:
        # Update url if currently empty
        remark42_match = re.search(r'(\[params\.remark42\][^\[]*?)url\s*=\s*""', content, re.DOTALL)
        if remark42_match:
            remark_url = f"{site_url}:{port}" if port not in ("80", "443") else site_url
            content = re.sub(
                r'(\[params\.remark42\][^\[]*?)url\s*=\s*""',
                rf'\1url = "{remark_url}"',
                content,
                flags=re.DOTALL,
            )
            with open(toml_path, "w") as f:
                f.write(content)
            return f"hugo.toml → remark42 url updated to {remark_url}"
        return "hugo.toml → [params.remark42] already configured"

    # Append remark42 section
    remark_url = f"{site_url}:{port}" if port not in ("80", "443") else site_url
    remark_section = f"""
# --- Remark42 Comments (self-hosted) ---
[params.remark42]
  url = "{remark_url}"
  site = "{site_id}"
  locale = "{locale}"
"""
    content += remark_section
    with open(toml_path, "w") as f:
        f.write(content)
    return f"hugo.toml → [params.remark42] added (url = {remark_url})"


def setup(data):
    """Setup trigger: download binary, create dirs, patch hugo.toml."""
    site_url = data.get("site_url", "").strip().rstrip("/")
    port = data.get("port", "8080")
    backup_path = data.get("backup_path", "").strip()

    base_path = get_base_path(data)
    if not base_path:
        return {"success": False, "error": "base_path not configured."}

    # Site URL: use input, fallback to server config base_url
    if not site_url:
        site_url = get_site_url()
    if not site_url:
        return {"success": False, "error": "Site URL not found. Configure url in server settings or enter manually."}

    # Backup path: use input, fallback to {base_path}/remark42/db
    if not backup_path:
        backup_path = os.path.join(base_path, "remark42", "db")

    log = []

    # 1. Download binary
    try:
        if os.path.isfile(REMARK42_BIN):
            log.append(f"Binary already exists: {REMARK42_BIN}")
        else:
            version = download_binary()
            log.append(f"Downloaded Remark42: {version}")
    except RuntimeError as e:
        return {"success": False, "error": str(e)}

    # 2. Create directories
    os.makedirs(REMARK42_VAR, exist_ok=True)
    log.append(f"Data dir: {REMARK42_VAR}")

    os.makedirs(backup_path, exist_ok=True)
    log.append(f"Backup dir: {backup_path}")

    # 3. Generate secret if not exists
    secret = secrets.token_hex(32)
    env_data = {
        "port": port,
        "secret": secret,
        "site": "blog",
        "backup_path": backup_path,
    }

    existing = load_env()
    if existing:
        # Keep existing secret
        env_data["secret"] = existing.get("secret", secret)

    save_env(env_data)
    log.append(f"Config saved: {REMARK42_ENV}")

    # 4. Patch hugo.toml
    toml_result = patch_hugo_toml(base_path, site_url, port)
    log.append(toml_result)

    summary = "\n".join(f"  • {l}" for l in log)
    manual_steps = """
Next steps (manual):
  1. Open port {port} on your server firewall
  2. (Optional) Set up systemd service for auto-start
  3. (Optional) Set up reverse proxy (nginx) for same-domain access
  4. Click "Restart Remark42" to start the server
  5. Deploy theme to apply comment widget""".format(port=port)

    return {
        "success": True,
        "message": "Remark42 setup complete",
        "actions": [
            {"type": "toast", "content": {"message": "Remark42 setup complete!", "toast_type": "success"}},
            {"type": "show_result", "content": {
                "title": "Remark42 Setup",
                "body": f"Setup completed:\n\n{summary}\n\n{manual_steps}",
            }},
        ],
    }


def read_remark_url_from_toml(base_path):
    """Read [params.remark42] url from hugo.toml."""
    toml_path = os.path.join(base_path, "hugo.toml")
    if not os.path.isfile(toml_path):
        return ""
    with open(toml_path, "r") as f:
        content = f.read()
    m = re.search(r'\[params\.remark42\][^\[]*?url\s*=\s*"([^"]*)"', content, re.DOTALL)
    return m.group(1) if m else ""


def restart(data):
    """Restart trigger: kill existing process, start new one."""
    env = load_env()
    if not env:
        return {"success": False, "error": "Remark42 not configured. Run Setup first."}

    if not os.path.isfile(REMARK42_BIN):
        return {"success": False, "error": f"Binary not found: {REMARK42_BIN}. Run Setup first."}

    # Kill existing
    run("pkill -f 'remark42 server' 2>/dev/null || true")

    port = env.get("port", "8080")
    secret = env.get("secret", "")
    site = env.get("site", "blog")
    backup_path = env.get("backup_path", "")

    # REMARK_URL from hugo.toml (source of truth)
    base_path = get_base_path(data)
    remark_url = read_remark_url_from_toml(base_path) if base_path else ""
    if not remark_url:
        return {"success": False, "error": "remark42 url not found in hugo.toml. Set [params.remark42] url first."}

    # Build command
    auth_anon = "true" if env.get("auth_anon", True) else "false"
    cmd = (
        f'REMARK_URL="{remark_url}" '
        f'SECRET="{secret}" '
        f'SITE="{site}" '
        f'ALLOWED_HOSTS="{get_site_url()}" '
        f'AUTH_ANON={auth_anon} '
        f'nohup "{REMARK42_BIN}" server '
        f'--port={port} '
        f'--store.bolt.path="{REMARK42_VAR}" '
        f'--backup="{REMARK42_AUTO_BACKUP}" '
        f'> "{REMARK42_LOG}" 2>&1 &'
    )

    rc, _ = run(cmd)

    # Wait a moment and check if process is running
    time.sleep(1)
    rc, pid = run("pgrep -f 'remark42 server' | head -1")

    if pid:
        return {
            "success": True,
            "message": f"Remark42 started (PID: {pid})",
            "actions": [
                {"type": "toast", "content": {"message": f"Remark42 running on port {port}", "toast_type": "success"}},
                {"type": "show_result", "content": {
                    "title": "Remark42 Server",
                    "body": (
                        f"Remark42 is running!\n\n"
                        f"  PID: {pid}\n"
                        f"  URL: {remark_url}\n"
                        f"  Site: {site}\n"
                        f"  Data: {REMARK42_VAR}\n"
                        f"  Backup: {backup_path}\n"
                        f"  Log: {REMARK42_LOG}"
                    ),
                }},
            ],
        }
    else:
        # Read last few lines of log for error
        _, log_tail = run(f'tail -20 "{REMARK42_LOG}" 2>/dev/null')
        return {
            "success": False,
            "error": "Remark42 failed to start",
            "actions": [
                {"type": "show_result", "content": {
                    "title": "Remark42 — Start Failed",
                    "body": f"Process did not start. Log output:\n\n{log_tail}",
                }},
            ],
        }


def backup(data):
    """Backup trigger: copy latest Remark42 backup to a fixed filename in base_path."""
    import glob as g
    import shutil

    env = load_env()
    if not env:
        return {"success": False, "error": "Remark42 not configured. Run Setup first."}

    base_path = get_base_path(data)
    if not base_path:
        return {"success": False, "error": "base_path not configured."}

    backup_path = env.get("backup_path", os.path.join(base_path, "remark42", "db"))

    # Find latest auto-backup from Remark42
    os.makedirs(REMARK42_AUTO_BACKUP, exist_ok=True)
    files = sorted(g.glob(os.path.join(REMARK42_AUTO_BACKUP, "*.gz")), key=os.path.getmtime, reverse=True)

    if not files:
        return {"success": False, "error": f"No backup files found in {REMARK42_AUTO_BACKUP}"}

    # Copy latest to fixed filename in base_path
    os.makedirs(backup_path, exist_ok=True)
    dst = os.path.join(backup_path, "backup.gz")
    shutil.copy2(files[0], dst)

    return {
        "success": True,
        "message": f"Backup saved: {dst}",
        "actions": [
            {"type": "toast", "content": {"message": "Remark42 backup complete", "toast_type": "success"}},
        ],
    }


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    trigger = data.get("trigger", "cron")

    if trigger == "cron":
        result = backup(data)
    elif trigger == "manual":
        if "site_url" in data:
            result = setup(data)
        else:
            result = restart(data)
    else:
        result = backup(data)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
