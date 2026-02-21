#!/usr/bin/env python3
"""Git auto-squash — squash commits by date range.

Manual trigger: specify since/until dates to squash commits in that range.
Cron "Monthly squash": auto-squash previous month's commits on the 1st.
"""

import json
import sys
import os
import subprocess
import shutil
from datetime import datetime, timedelta


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
        return config.get("hugo_config", {}).get("base_path", "")
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
            "content": {"title": f"Git Auto-Squash — {title}", "body": body},
        }],
    }


def handle_push_error(stderr):
    if "Permission denied" in stderr or "publickey" in stderr:
        pubkey = None
        for key_file in ["~/.ssh/id_ed25519.pub", "~/.ssh/id_rsa.pub"]:
            path = os.path.expanduser(key_file)
            if os.path.isfile(path):
                with open(path, "r") as f:
                    pubkey = f.read().strip()
                break

        if pubkey:
            body = (
                "Force push failed: permission denied (publickey).\n\n"
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
                "Force push failed: permission denied (publickey).\n\n"
                "No SSH key found on the server.\n\n"
                "Run the following on the server:\n\n"
                "  ssh-keygen -t ed25519 -C \"server\"\n"
                "  cat ~/.ssh/id_ed25519.pub\n\n"
                "Then add the key to your Git provider.\n"
            )
        return result_guide("SSH Key Authentication Failed", body)

    return result_toast(f"Force push failed: {stderr}")


def get_prev_month_range():
    """Return (since, until, label) for previous month."""
    today = datetime.now()
    first_of_this_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    first_of_prev_month = (first_of_this_month - timedelta(days=1)).replace(day=1)

    since = first_of_prev_month.strftime("%Y-%m-%d")
    until = first_of_this_month.strftime("%Y-%m-%d")
    label = first_of_prev_month.strftime("%Y-%m")
    return since, until, label


def do_squash(base_path, since, until, range_label):
    """Squash commits in [since, until) range."""
    os.chdir(base_path)

    # Check git repo
    ret = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if ret.returncode != 0:
        return result_toast(f"Not a git repository: {base_path}")

    # Check remote
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    if remote.returncode != 0 or not remote.stdout.strip():
        return result_guide(
            "No Remote Configured",
            "Git remote 'origin' is not set.\n\n"
            "  git remote add origin git@github.com:user/repo.git\n",
        )

    # Get current branch
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    # Find commits in range (oldest first)
    log = subprocess.run(
        ["git", "log", f"--since={since}", f"--until={until}",
         "--format=%H %ci", "--reverse"],
        capture_output=True, text=True,
    )
    lines = [l for l in log.stdout.strip().split("\n") if l.strip()]

    if len(lines) == 0:
        return {"success": True, "message": f"No commits found in {since} ~ {until}"}

    if len(lines) == 1:
        return {"success": True, "message": f"Only 1 commit in {since} ~ {until}, no squash needed"}

    count = len(lines)
    first_hash = lines[0].split()[0]
    first_date = lines[0].split()[1] + " " + lines[0].split()[2]
    last_hash = lines[-1].split()[0]
    last_date = lines[-1].split()[1] + " " + lines[-1].split()[2]

    # Get parent of first commit
    parent_result = subprocess.run(
        ["git", "rev-parse", f"{first_hash}^"],
        capture_output=True, text=True,
    )
    if parent_result.returncode != 0:
        return result_toast("Cannot squash: first commit in range has no parent")
    parent_hash = parent_result.stdout.strip()

    msg = f"[{range_label}] squash {count} commits ({first_date} ~ {last_date})"

    # Check if there are commits after last_hash
    newer = subprocess.run(
        ["git", "log", f"{last_hash}..HEAD", "--format=%H"],
        capture_output=True, text=True,
    ).stdout.strip()

    if not newer:
        # Simple: HEAD is the last commit in range
        subprocess.run(["git", "reset", "--soft", parent_hash], check=True)
        subprocess.run(
            ["git", "commit", "-m", msg, "--no-gpg-sign"],
            capture_output=True, text=True,
        )
    else:
        # Preserve newer commits via rebase
        subprocess.run(
            ["git", "checkout", "-b", "inn_temp_squash", last_hash],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(["git", "reset", "--soft", parent_hash], check=True)
        subprocess.run(
            ["git", "commit", "-m", msg, "--no-gpg-sign"],
            capture_output=True, text=True,
        )

        subprocess.run(
            ["git", "checkout", branch],
            check=True, capture_output=True, text=True,
        )
        rebase = subprocess.run(
            ["git", "rebase", "--onto", "inn_temp_squash", last_hash, branch],
            capture_output=True, text=True,
        )
        if rebase.returncode != 0:
            subprocess.run(["git", "rebase", "--abort"], capture_output=True, text=True)
            subprocess.run(["git", "checkout", branch], capture_output=True, text=True)
            subprocess.run(["git", "branch", "-D", "inn_temp_squash"], capture_output=True, text=True)
            return result_toast("Squash failed: rebase conflict")

        subprocess.run(
            ["git", "branch", "-D", "inn_temp_squash"],
            capture_output=True, text=True,
        )

    # Force push
    push = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", "HEAD"],
        capture_output=True, text=True,
        timeout=30,
    )
    if push.returncode != 0:
        return handle_push_error(push.stderr.strip())

    return {
        "success": True,
        "message": msg,
        "actions": [
            {"type": "toast", "content": {"message": msg, "toast_type": "success"}},
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

    if not shutil.which("git"):
        print(json.dumps(result_toast("Git is not installed on the server.")))
        return

    base_path = get_base_path(data)

    if not base_path:
        config_example = json.dumps({
            "hugo_config": {
                "base_path": "/home/user/my-blog",
                "content_path": "posts",
                "image_path": "static",
            }
        }, indent=2)
        print(json.dumps(result_guide(
            "Configuration Required",
            "base_path not configured.\n\n"
            "Set hugo_config.base_path in ~/.inn_server_config.json:\n\n"
            f"{config_example}\n",
        )))
        return

    if not os.path.isdir(base_path):
        print(json.dumps(result_toast(f"Path not found: {base_path}")))
        return

    # Determine date range
    if trigger == "manual":
        since = data.get("since", "").strip()
        until = data.get("until", "").strip()

        if not since or not until:
            print(json.dumps(result_toast("Both 'From' and 'To' dates are required.")))
            return

        # Validate date format
        try:
            datetime.strptime(since, "%Y-%m-%d")
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            print(json.dumps(result_toast("Invalid date format. Use YYYY-MM-DD.")))
            return

        if since >= until:
            print(json.dumps(result_toast("'From' must be before 'To'.")))
            return

        range_label = f"{since} ~ {until}"
    else:
        # Cron: previous month
        since, until, range_label = get_prev_month_range()

    result = do_squash(base_path, since, until, range_label)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
