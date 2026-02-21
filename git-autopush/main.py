#!/usr/bin/env python3
"""Git auto-push — commit and push with timestamp.

Manual trigger: button click → commit & push.
Cron "Auto push": periodic commit & push.
"""

import json
import sys
import os
import subprocess
import shutil
from datetime import datetime


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


def result_toast(message):
    return {"success": False, "error": message}


def result_guide(title, body):
    return {
        "success": False,
        "error": title,
        "actions": [{
            "type": "show_result",
            "content": {"title": f"Git Auto-Push — {title}", "body": body},
        }],
    }


def handle_push_error(stderr):
    if "Permission denied" in stderr or "publickey" in stderr or "Host key verification failed" in stderr:
        pubkey = None
        for key_file in ["~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"]:
            path = os.path.expanduser(key_file)
            if os.path.isfile(path):
                with open(path, "r") as f:
                    pubkey = f.read().strip()
                break

        if pubkey:
            body = (
                "Git push failed: permission denied (publickey).\n\n"
                "SSH key found but not registered with your Git provider.\n"
                "Copy the key below and add it:\n\n"
                "  GitHub:  Settings > SSH and GPG keys > New SSH key\n"
                "  GitLab:  Preferences > SSH Keys\n\n"
                f"{{{{copy:Public Key}}}}\n{pubkey}\n{{{{/copy}}}}\n\n"
                "After registering, test with:\n"
                "  ssh -T git@github.com\n"
            )
        else:
            body = (
                "Git push failed: permission denied (publickey).\n\n"
                "No SSH key found on the server.\n\n"
                "Run the following on the server:\n\n"
                "  # 1. Generate SSH key\n"
                "  ssh-keygen -t ed25519 -C \"inn-autopush-plugin\"\n\n"
                "  # 2. Copy the public key\n"
                "  cat ~/.ssh/id_ed25519.pub\n\n"
                "  # 3. Add it to your Git provider\n"
                "  GitHub:  Settings > SSH and GPG keys > New SSH key\n"
                "  GitLab:  Preferences > SSH Keys\n\n"
                "  # 4. Test connection\n"
                "  ssh -T git@github.com\n"
            )
        return result_guide("SSH Key Authentication Failed", body)

    return result_toast(f"Push failed: {stderr}")


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    trigger = data.get("trigger", "cron")

    if not shutil.which("git"):
        print(json.dumps(result_toast("Git is not installed on the server.")))
        return

    base_path = get_base_path(data)

    if not base_path:
        config_example = json.dumps({
            "cms_config": {
                "hugo_config": {
                    "base_path": "/home/user/my-blog",
                    "content_path": "posts",
                    "image_path": "static",
                }
            }
        }, indent=2)
        print(json.dumps(result_guide(
            "Configuration Required",
            "base_path not configured.\n\n"
            "Set cms_config.hugo_config.base_path in ~/.inn_server_config.json:\n\n"
            f"{config_example}\n",
        )))
        return

    if not os.path.isdir(base_path):
        print(json.dumps(result_toast(f"Path not found: {base_path}")))
        return

    os.chdir(base_path)

    # Check git repo
    ret = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        print(json.dumps(result_toast(f"Not a git repository: {base_path}")))
        return

    # Check remote
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        print(json.dumps(result_guide(
            "No Remote Configured",
            "Git remote 'origin' is not set.\n\n"
            "Run the following on the server:\n\n"
            "  git remote add origin <your-repo-url>\n\n"
            "Examples:\n"
            "  git remote add origin git@github.com:user/repo.git\n"
            "  git remote add origin https://github.com/user/repo.git\n",
        )))
        return

    # Check for changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print(json.dumps({"success": True, "message": "No changes to commit"}))
        return

    subprocess.run(["git", "add", "-A"], check=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if trigger == "manual":
        msg = f"[{now}] sync"
    else:
        msg = f"[{now}] auto"

    commit = subprocess.run(
        ["git", "commit", "-m", msg, "--no-gpg-sign"],
        capture_output=True, text=True,
    )
    if commit.returncode != 0:
        stderr = commit.stderr.strip()
        if "Please tell me who you are" in stderr or "user.name" in stderr:
            print(json.dumps(result_guide(
                "Git User Not Configured",
                "Git user.name/email not set.\n\n"
                "Run the following on the server:\n\n"
                "  git config --global user.name \"Your Name\"\n"
                "  git config --global user.email \"you@example.com\"\n",
            )))
        else:
            print(json.dumps(result_toast(f"Commit failed: {stderr}")))
        return

    push = subprocess.run(
        ["git", "push", "origin", "HEAD"],
        capture_output=True, text=True,
        timeout=30,
    )
    if push.returncode != 0:
        print(json.dumps(handle_push_error(push.stderr.strip())))
        return

    print(json.dumps({
        "success": True,
        "message": msg,
        "actions": [
            {"type": "refresh_tree"},
            {"type": "toast", "content": {"message": f"Git: {msg}", "toast_type": "success"}},
        ],
    }))


if __name__ == "__main__":
    main()
