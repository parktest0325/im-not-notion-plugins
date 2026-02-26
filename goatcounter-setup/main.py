#!/usr/bin/env python3
"""Install and manage GoatCounter self-hosted analytics server."""

import glob
import json
import os
import subprocess
import sys
import re
import tarfile
import time

GOATCOUNTER_DIR = os.path.expanduser("~/.goatcounter")
GOATCOUNTER_BIN = os.path.join(GOATCOUNTER_DIR, "goatcounter")
GOATCOUNTER_DB = os.path.join(GOATCOUNTER_DIR, "goatcounter.db")
GOATCOUNTER_LOG = os.path.join(GOATCOUNTER_DIR, "goatcounter.log")
GOATCOUNTER_ENV = os.path.join(GOATCOUNTER_DIR, "env.json")


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
    """Get site URL from server config."""
    try:
        config = load_server_config()
        url = config.get("cms_config", {}).get("hugo_config", {}).get("url", "")
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    return ""


def run(cmd, check=False):
    """Run shell command, return (returncode, output)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{r.stderr}")
    output = r.stdout.strip() or r.stderr.strip()
    return r.returncode, output


def detect_arch():
    _, arch = run("uname -m")
    mapping = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "arm",
    }
    return mapping.get(arch, arch)


def detect_os():
    _, osname = run("uname -s")
    return osname.lower()


def get_latest_version():
    """Get latest GoatCounter version tag from GitHub."""
    rc, output = run("curl -fSsI -o /dev/null -w '%{redirect_url}' https://github.com/arp242/goatcounter/releases/latest")
    if rc == 0 and output:
        # Extract version from redirect URL (e.g., .../tag/v2.5.0)
        version = output.rstrip("/").split("/")[-1]
        return version
    return "v2.5.0"  # fallback


def download_binary():
    """Download GoatCounter binary from GitHub releases."""
    arch = detect_arch()
    osname = detect_os()
    version = get_latest_version()

    filename = f"goatcounter-{version}-{osname}-{arch}.gz"
    url = f"https://github.com/arp242/goatcounter/releases/download/{version}/{filename}"
    tmp = f"/tmp/{filename}"

    os.makedirs(GOATCOUNTER_DIR, exist_ok=True)

    # Download
    rc, _ = run(f'curl -fSL -o "{tmp}" "{url}"')
    if rc != 0:
        raise RuntimeError(f"Failed to download GoatCounter from {url}")

    # Extract (gzip, not tar)
    rc, _ = run(f'gunzip -f "{tmp}"')
    if rc != 0:
        raise RuntimeError(f"Failed to extract {tmp}")

    # Move binary
    extracted = tmp.replace(".gz", "")
    if os.path.isfile(extracted):
        if os.path.isfile(GOATCOUNTER_BIN):
            os.remove(GOATCOUNTER_BIN)
        os.rename(extracted, GOATCOUNTER_BIN)

    # Ensure executable
    run(f'chmod +x "{GOATCOUNTER_BIN}"')

    # Verify
    rc, version_out = run(f'"{GOATCOUNTER_BIN}" version 2>&1 || true')
    return version_out or version


def save_env(env_data):
    with open(GOATCOUNTER_ENV, "w") as f:
        json.dump(env_data, f, indent=2)


def load_env():
    if not os.path.isfile(GOATCOUNTER_ENV):
        return None
    with open(GOATCOUNTER_ENV, "r") as f:
        return json.load(f)


def read_endpoint_from_toml(base_path):
    """Read goatcounterEndpoint from hugo.toml."""
    toml_path = os.path.join(base_path, "hugo.toml")
    if not os.path.isfile(toml_path):
        return ""
    with open(toml_path, "r") as f:
        content = f.read()
    m = re.search(r'goatcounterEndpoint\s*=\s*"([^"]*)"', content)
    return m.group(1) if m else ""


def patch_hugo_toml(base_path, public_url):
    """Patch goatcounterEndpoint in hugo.toml."""
    toml_path = os.path.join(base_path, "hugo.toml")
    if not os.path.isfile(toml_path):
        return "hugo.toml not found — skipped"

    with open(toml_path, "r") as f:
        content = f.read()

    # Check if goatcounterEndpoint exists
    if "goatcounterEndpoint" in content:
        # Update if currently empty
        m = re.search(r'goatcounterEndpoint\s*=\s*""', content)
        if m:
            content = re.sub(
                r'goatcounterEndpoint\s*=\s*""',
                f'goatcounterEndpoint = "{public_url}"',
                content,
            )
            with open(toml_path, "w") as f:
                f.write(content)
            return f"hugo.toml → goatcounterEndpoint = {public_url}"
        return "hugo.toml → goatcounterEndpoint already configured"

    # Append
    section = f'\n# --- GoatCounter Analytics (self-hosted) ---\n  goatcounterEndpoint = "{public_url}"\n  goatcounterShow = true\n'
    # Insert under [params] if it exists
    if "[params]" in content:
        content = content.replace("[params]", f"[params]{section}", 1)
    else:
        content += f"\n[params]{section}"

    with open(toml_path, "w") as f:
        f.write(content)
    return f"hugo.toml → goatcounterEndpoint added ({public_url})"


def setup(data):
    """Setup trigger: download binary, create DB, patch hugo.toml."""
    site_url = data.get("site_url", "").strip().rstrip("/")
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    port = data.get("port", "8081")
    backup_path = data.get("backup_path", "").strip()

    base_path = get_base_path(data)
    if not base_path:
        return {"success": False, "error": "base_path not configured."}

    # Backup path: use input, fallback to {base_path}/goatcounter/backup
    if not backup_path:
        backup_path = os.path.join(base_path, "goatcounter", "backup")

    # Public URL is required — user must specify the actual access URL
    if not site_url:
        return {"success": False, "error": "Public URL is required. Enter the URL where GoatCounter will be accessed (e.g. https://stat.example.com)."}

    if not email:
        return {"success": False, "error": "Admin email is required."}

    if not password:
        return {"success": False, "error": "Admin password is required."}

    public_url = site_url

    log = []

    # 1. Download binary
    try:
        if os.path.isfile(GOATCOUNTER_BIN):
            log.append(f"Binary already exists: {GOATCOUNTER_BIN}")
        else:
            version = download_binary()
            log.append(f"Downloaded GoatCounter: {version}")
    except RuntimeError as e:
        return {"success": False, "error": str(e)}

    # 2. Create DB and site
    db_conn = f"sqlite3+{GOATCOUNTER_DB}"
    vhost = public_url.replace("https://", "").replace("http://", "")

    rc, out = run(
        f'"{GOATCOUNTER_BIN}" db create site '
        f'-db "{db_conn}" '
        f'-createdb '
        f'-vhost "{vhost}" '
        f'-user.email "{email}" '
        f'-user.password "{password}"'
    )
    if rc != 0:
        return {
            "success": False,
            "error": "DB create failed",
            "actions": [
                {"type": "show_result", "content": {
                    "title": "GoatCounter Setup — Failed",
                    "body": (
                        f"Failed to create DB with vhost: {vhost}\n\n"
                        f"Error: {out}\n\n"
                        f"If re-configuring, stop the server and remove the old DB first, then run Setup again.\n\n"
                        f"```\n"
                        f"pkill -f 'goatcounter serve'\n"
                        f"rm {GOATCOUNTER_DB}\n"
                        f"```"
                    ),
                }},
            ],
        }
    log.append(f"DB created: {GOATCOUNTER_DB} (vhost: {vhost})")

    # Enable public counter API by default
    rc2, _ = run(
        f'"{GOATCOUNTER_BIN}" db query '
        f'-db "{db_conn}" '
        f"\"UPDATE sites SET settings = json_set(settings, '$.allow_counter', json('true'))\""
    )
    if rc2 == 0:
        log.append("Counter API enabled")

    # 3. Create backup directory
    os.makedirs(backup_path, exist_ok=True)
    log.append(f"Backup dir: {backup_path}")

    # 4. Save env
    env_data = {
        "port": port,
        "db_path": GOATCOUNTER_DB,
        "backup_path": backup_path,
        "email": email,
    }

    existing = load_env()
    if existing:
        env_data.update({k: v for k, v in existing.items() if k not in env_data})

    save_env(env_data)
    log.append(f"Config saved: {GOATCOUNTER_ENV}")

    # 5. Patch hugo.toml
    toml_result = patch_hugo_toml(base_path, public_url)
    log.append(toml_result)

    summary = "\n".join(f"  • {l}" for l in log)
    manual_steps = f"""
Admin: {email}

Next steps:
  1. Edit hugo.toml goatcounterEndpoint to your public URL if using reverse proxy
  2. Click "Restart GoatCounter" to start the server
  3. Rebuild Hugo to enable analytics widget
  4. (Optional) Set up reverse proxy for subdomain access (e.g. stats.yourdomain.com)"""

    return {
        "success": True,
        "message": "GoatCounter setup complete",
        "actions": [
            {"type": "toast", "content": {"message": "GoatCounter setup complete!", "toast_type": "success"}},
            {"type": "show_result", "content": {
                "title": "GoatCounter Setup",
                "body": f"Setup completed:\n\n{summary}\n\n{manual_steps}",
            }},
        ],
    }


def restart(data):
    """Restart trigger: kill existing process, start new one."""
    env = load_env()
    if not env:
        return {"success": False, "error": "GoatCounter not configured. Run Setup first."}

    if not os.path.isfile(GOATCOUNTER_BIN):
        return {"success": False, "error": f"Binary not found: {GOATCOUNTER_BIN}. Run Setup first."}

    # Kill existing
    run("pkill -f 'goatcounter serve' 2>/dev/null || true")

    port = env.get("port", "8081")
    db_path = env.get("db_path", GOATCOUNTER_DB)
    db_conn = f"sqlite3+{db_path}"

    # Read endpoint from hugo.toml (source of truth)
    base_path = get_base_path(data)
    endpoint = read_endpoint_from_toml(base_path) if base_path else ""
    if not endpoint:
        return {"success": False, "error": "goatcounterEndpoint not found in hugo.toml. Set it first."}

    # Check for GeoIP City database in plugin directory
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    geoip_path = os.path.join(plugin_dir, "GeoLite2-City.mmdb")

    # Auto-extract .mmdb from .tar.gz if not already extracted
    if not os.path.isfile(geoip_path):
        tgz_files = glob.glob(os.path.join(plugin_dir, "GeoLite2-City*.tar.gz"))
        for tgz in tgz_files:
            try:
                with tarfile.open(tgz, "r:gz") as tar:
                    for member in tar.getmembers():
                        if member.name.endswith("GeoLite2-City.mmdb"):
                            member.name = os.path.basename(member.name)
                            tar.extract(member, plugin_dir)
                            break
                if os.path.isfile(geoip_path):
                    os.remove(tgz)
                    break
            except Exception:
                pass

    geoip_flag = f'-geodb "{geoip_path}" ' if os.path.isfile(geoip_path) else ""

    # Build command
    cmd = (
        f'nohup "{GOATCOUNTER_BIN}" serve '
        f'-listen :{port} '
        f'-tls none '
        f'-db "{db_conn}" '
        f'{geoip_flag}'
        f'> "{GOATCOUNTER_LOG}" 2>&1 &'
    )

    rc, _ = run(cmd)

    # Wait and check
    time.sleep(1)
    rc, pid = run("pgrep -f 'goatcounter serve' | head -1")

    if pid:
        return {
            "success": True,
            "message": f"GoatCounter started (PID: {pid})",
            "actions": [
                {"type": "toast", "content": {"message": f"GoatCounter running on port {port}", "toast_type": "success"}},
                {"type": "show_result", "content": {
                    "title": "GoatCounter Server",
                    "body": (
                        f"GoatCounter is running!\n\n"
                        f"  PID: {pid}\n"
                        f"  Endpoint: {endpoint}\n"
                        f"  Port: {port}\n"
                        f"  DB: {db_path}\n"
                        f"  Log: {GOATCOUNTER_LOG}"
                    ),
                }},
            ],
        }
    else:
        _, log_tail = run(f'tail -20 "{GOATCOUNTER_LOG}" 2>/dev/null')
        return {
            "success": False,
            "error": "GoatCounter failed to start",
            "actions": [
                {"type": "show_result", "content": {
                    "title": "GoatCounter — Start Failed",
                    "body": f"Process did not start. Log output:\n\n{log_tail}",
                }},
            ],
        }


def backup(data):
    """Backup trigger: export GoatCounter DB to CSV."""
    env = load_env()
    if not env:
        return {"success": False, "error": "GoatCounter not configured. Run Setup first."}

    db_path = env.get("db_path", GOATCOUNTER_DB)
    backup_path = env.get("backup_path", "")

    if not backup_path:
        base_path = get_base_path(data)
        if base_path:
            backup_path = os.path.join(base_path, "goatcounter", "backup")
        else:
            return {"success": False, "error": "backup_path not configured."}

    os.makedirs(backup_path, exist_ok=True)

    db_conn = f"sqlite3+{db_path}"
    export_file = os.path.join(backup_path, "goatcounter.csv")

    rc, out = run(f'"{GOATCOUNTER_BIN}" db export -db "{db_conn}" -format csv > "{export_file}"')
    if rc != 0:
        rc, out = run(f'"{GOATCOUNTER_BIN}" db export -db "{db_conn}" > "{export_file}"')
        if rc != 0:
            return {"success": False, "error": f"Export failed: {out}"}

    return {
        "success": True,
        "message": f"Backup saved: {export_file}",
        "actions": [
            {"type": "toast", "content": {"message": "GoatCounter backup complete", "toast_type": "success"}},
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
