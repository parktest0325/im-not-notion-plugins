#!/usr/bin/env python3
"""Install and manage Remark42 self-hosted comment server."""

import json
import os
import subprocess
import sys
import secrets
import re

REMARK42_DIR = os.path.expanduser("~/.remark42")
REMARK42_BIN = os.path.join(REMARK42_DIR, "remark42")
REMARK42_VAR = os.path.join(REMARK42_DIR, "var")
REMARK42_LOG = os.path.join(REMARK42_DIR, "remark42.log")
REMARK42_ENV = os.path.join(REMARK42_DIR, "env.json")
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
    filename = f"remark42-{osname}-{arch}.tar.gz"
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
        if re.search(r'url\s*=\s*""', content):
            remark_url = f"{site_url}:{port}" if port != "80" and port != "443" else site_url
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
    inp = data.get("input", {})
    site_url = inp.get("site_url", "").rstrip("/")
    port = inp.get("port", "8080")
    backup_rel = inp.get("backup_path", "remark42/db")

    base_path = get_base_path(data)
    if not base_path:
        return {"success": False, "error": "base_path not configured."}

    if not site_url:
        return {"success": False, "error": "Site URL is required."}

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

    backup_path = os.path.join(base_path, backup_rel)
    os.makedirs(backup_path, exist_ok=True)
    log.append(f"Backup dir: {backup_path}")

    # 3. Generate secret if not exists
    secret = secrets.token_hex(32)
    env_data = {
        "site_url": site_url,
        "port": port,
        "secret": secret,
        "site": "blog",
        "backup_path": backup_path,
        "base_path": base_path,
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


def restart(data):
    """Restart trigger: kill existing process, start new one."""
    env = load_env()
    if not env:
        return {"success": False, "error": "Remark42 not configured. Run Setup first."}

    if not os.path.isfile(REMARK42_BIN):
        return {"success": False, "error": f"Binary not found: {REMARK42_BIN}. Run Setup first."}

    # Kill existing
    run("pkill -f 'remark42 server' 2>/dev/null || true")

    site_url = env.get("site_url", "")
    port = env.get("port", "8080")
    secret = env.get("secret", "")
    site = env.get("site", "blog")
    backup_path = env.get("backup_path", "")

    remark_url = f"{site_url}:{port}" if port not in ("80", "443") else site_url

    # Build command
    cmd = (
        f'REMARK_URL="{remark_url}" '
        f'SECRET="{secret}" '
        f'SITE="{site}" '
        f'ALLOWED_HOSTS="{site_url}" '
        f'nohup "{REMARK42_BIN}" server '
        f'--port={port} '
        f'--store.bolt.path="{REMARK42_VAR}" '
        f'--backup="{backup_path}" '
        f'> "{REMARK42_LOG}" 2>&1 &'
    )

    rc, _ = run(cmd)

    # Wait a moment and check if process is running
    import time
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


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    trigger = data.get("trigger", "manual")
    label = ""

    # Determine which manual trigger was used
    if trigger == "manual":
        inp = data.get("input", {})
        # If site_url field exists in input, it's the Setup trigger
        if "site_url" in inp:
            result = setup(data)
        else:
            result = restart(data)
    else:
        # Cron or other — default to restart
        result = restart(data)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
