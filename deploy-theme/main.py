#!/usr/bin/env python3
"""Deploy im-not-notion-theme + demo content to the Hugo site."""

import json
import sys
import os
import re
import shutil
import subprocess


# Hugo override folders — site-level copies override theme equivalents
HUGO_OVERRIDE_DIRS = ["layouts", "archetypes", "assets", "i18n", "data"]


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


def result_toast(message, success=False):
    return {"success": success, "error": message} if not success else {"success": True, "message": message}


def copytree(src, dst):
    """Copy directory tree, overwriting existing files."""
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for entry in os.scandir(src):
        s = entry.path
        d = os.path.join(dst, entry.name)
        if entry.is_dir(follow_symlinks=False):
            copytree(s, d)
        else:
            shutil.copy2(s, d)


def run(cmd, cwd=None):
    """Run shell command, return (returncode, stdout, stderr)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def remove_submodule(base_path, theme_rel):
    """Remove existing git submodule if present."""
    run(f"git submodule deinit -f {theme_rel}", cwd=base_path)
    run(f"git rm -rf {theme_rel}", cwd=base_path)
    git_modules = os.path.join(base_path, ".git", "modules", theme_rel)
    if os.path.isdir(git_modules):
        shutil.rmtree(git_modules)


def deploy_theme_submodule(base_path, theme_url, theme_dst, theme_rel, actions_log):
    """Deploy theme as git submodule."""
    # Remove existing (submodule or plain directory)
    remove_submodule(base_path, theme_rel)
    if os.path.isdir(theme_dst):
        shutil.rmtree(theme_dst)

    # Add submodule
    rc, out, err = run(f"git submodule add {theme_url} {theme_rel}", cwd=base_path)
    if rc != 0:
        actions_log.append(f"Submodule add failed: {err}")
        return False

    actions_log.append(f"Theme → submodule {theme_url}")
    return True


def deploy_theme_copy(theme_src, theme_dst, actions_log):
    """Deploy theme by copying bundled files."""
    if os.path.isdir(theme_dst):
        shutil.rmtree(theme_dst)
    os.makedirs(theme_dst, exist_ok=True)
    for entry in os.scandir(theme_src):
        if entry.name in ("exampleSite", ".git"):
            continue
        s = entry.path
        d = os.path.join(theme_dst, entry.name)
        if entry.is_dir(follow_symlinks=False):
            copytree(s, d)
        else:
            shutil.copy2(s, d)
    actions_log.append(f"Theme → {theme_dst} (copied)")


def deploy_demo_content(theme_src, base_path, actions_log):
    """Option 1: Deploy exampleSite/content/ to base_path/content/."""
    example_dir = os.path.join(theme_src, "exampleSite")
    content_src = os.path.join(example_dir, "content")
    if not os.path.isdir(content_src):
        actions_log.append("Content — exampleSite/content/ not found, skipped")
        return

    content_dst = os.path.join(base_path, "content")

    # Rename old → copy new → delete old
    old_content = None
    if os.path.isdir(content_dst):
        old_content = content_dst + "_old"
        if os.path.exists(old_content):
            shutil.rmtree(old_content)
        os.rename(content_dst, old_content)

    shutil.copytree(content_src, content_dst)

    if old_content and os.path.isdir(old_content):
        shutil.rmtree(old_content)
    actions_log.append(f"Content → {content_dst}")

    # Patch content_paths in server config to match demo sections
    demo_sections = []
    for entry in os.scandir(content_dst):
        if entry.is_dir() and not entry.name.startswith("_"):
            demo_sections.append(entry.name)
    demo_sections.sort()

    if demo_sections:
        config_path = os.path.expanduser("~/.inn_server_config.json")
        try:
            config = load_server_config()
            hugo_cfg = config.get("cms_config", {}).get("hugo_config", {})
            old_paths = hugo_cfg.get("content_paths", [])
            hugo_cfg["content_paths"] = demo_sections
            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)
            actions_log.append(f"content_paths → {demo_sections} (was {old_paths})")
        except Exception as e:
            actions_log.append(f"content_paths patch failed: {e}")


def overwrite_hugo_toml(theme_src, base_path, actions_log):
    """Option 2: Overwrite hugo.toml with exampleSite version, patching baseURL."""
    example_dir = os.path.join(theme_src, "exampleSite")
    hugo_toml_src = os.path.join(example_dir, "hugo.toml")
    hugo_toml_dst = os.path.join(base_path, "hugo.toml")

    if not os.path.isfile(hugo_toml_src):
        actions_log.append("hugo.toml — exampleSite/hugo.toml not found, skipped")
        return

    shutil.copy2(hugo_toml_src, hugo_toml_dst)

    # Patch baseURL from server config
    try:
        config = load_server_config()
        domain = config.get("cms_config", {}).get("hugo_config", {}).get("url", "")
        if domain:
            with open(hugo_toml_dst, "r") as f:
                content = f.read()
            content = re.sub(
                r'baseURL\s*=\s*"[^"]*"',
                f'baseURL = "{domain}"',
                content,
            )
            with open(hugo_toml_dst, "w") as f:
                f.write(content)
            actions_log.append(f"hugo.toml → overwritten (baseURL = {domain})")
        else:
            actions_log.append("hugo.toml → overwritten (baseURL from example)")
    except Exception:
        actions_log.append("hugo.toml → overwritten (baseURL from example)")


def clean_override_folders(base_path, actions_log):
    """Option 3: Remove Hugo override folders from site root."""
    removed = []
    for dirname in HUGO_OVERRIDE_DIRS:
        dirpath = os.path.join(base_path, dirname)
        if os.path.isdir(dirpath):
            shutil.rmtree(dirpath)
            removed.append(dirname)

    if removed:
        actions_log.append(f"Cleaned overrides → {', '.join(removed)}")
    else:
        actions_log.append("Clean overrides — no override folders found")


def main():
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            pass

    base_path = get_base_path(data)
    if not base_path:
        print(json.dumps(result_toast("base_path not configured.")))
        return

    if not os.path.isdir(base_path):
        print(json.dumps(result_toast(f"Path not found: {base_path}")))
        return

    # Read options
    theme_url = data.get("theme_url", "").strip()
    opt_deploy_content = str(data.get("deploy_content", "false")).lower() == "true"
    opt_overwrite_toml = str(data.get("overwrite_toml", "false")).lower() == "true"
    opt_clean_overrides = str(data.get("clean_overrides", "false")).lower() == "true"

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    theme_rel = os.path.join("themes", "im-not-notion-theme")
    theme_dst = os.path.join(base_path, theme_rel)

    actions_log = []

    # Deploy theme: submodule (url provided) or copy (bundled)
    if theme_url:
        if not deploy_theme_submodule(base_path, theme_url, theme_dst, theme_rel, actions_log):
            print(json.dumps(result_toast(f"Failed to add submodule: {theme_url}")))
            return
        theme_src = theme_dst
    else:
        theme_src = os.path.join(plugin_dir, "im-not-notion-theme")
        if not os.path.isdir(theme_src):
            print(json.dumps(result_toast(f"Theme not found: {theme_src}")))
            return
        deploy_theme_copy(theme_src, theme_dst, actions_log)

    # Option 1: deploy demo content
    if opt_deploy_content:
        deploy_demo_content(theme_src, base_path, actions_log)

    # Option 2: overwrite hugo.toml
    if opt_overwrite_toml:
        overwrite_hugo_toml(theme_src, base_path, actions_log)

    # Option 3: clean Hugo override folders
    if opt_clean_overrides:
        clean_override_folders(base_path, actions_log)

    summary = "\n".join(f"  • {a}" for a in actions_log)
    print(json.dumps({
        "success": True,
        "message": "Theme deployed",
        "actions": [
            {"type": "toast", "content": {"message": "Theme deployed!", "toast_type": "success"}},
            {"type": "show_result", "content": {
                "title": "Deploy Theme — Complete",
                "body": f"Deployed to {base_path}\n\n{summary}",
            }},
            {"type": "refresh_tree"},
        ],
    }))


if __name__ == "__main__":
    main()
