import re
from os.path import join, exists
from os import makedirs, getcwd
from pathlib import Path
from bomiot_token import encrypt_info
import sys


def _read_launcher_meta():
    """Read app_name and version from launcher.py in the current directory."""
    app_name = None
    version = None
    launcher_path = join(getcwd(), "launcher.py")
    if not exists(launcher_path):
        raise RuntimeError(f"launcher.py not found: {launcher_path}")
    with open(launcher_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r'^app_name\s*=\s*"([^"]+)"', line)
            if m:
                app_name = m.group(1)
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                version = m.group(1)
    if not app_name:
        raise RuntimeError("Cannot read app_name from launcher.py")
    if not version:
        raise RuntimeError("Cannot read version from launcher.py")
    return app_name, version


def _inject_env(yaml_content, env_vars):
    """Inject or update a top-level ``env:`` block in a GitHub Actions workflow.

    If a top-level ``env:`` block already exists, the keys in ``env_vars`` are
    updated in place (existing values are overwritten; missing keys are added).
    Otherwise a new ``env:`` block is inserted right after the ``name:`` line.
    """
    target_keys = set(env_vars.keys())
    lines = yaml_content.split("\n")
    out = []
    i = 0
    found_env = False

    while i < len(lines):
        line = lines[i]
        # A top-level env block starts with "env:" at column 0.
        if line.strip() == "env:":
            found_env = True
            out.append(line)
            i += 1
            # Walk through indented entries of this env block.
            seen = set()
            while i < len(lines):
                entry = lines[i]
                if entry and not entry[0].isspace():
                    # End of the env block.
                    break
                m = re.match(r'^(\s*)([A-Z_][A-Z0-9_]*)\s*:\s*.*$', entry)
                if m and m.group(2) in target_keys:
                    indent = m.group(1)
                    key = m.group(2)
                    out.append(f'{indent}{key}: "{env_vars[key]}"')
                    seen.add(key)
                else:
                    out.append(entry)
                i += 1
            # Append any missing keys at the end of the env block.
            last_indent = "  "
            for key in target_keys - seen:
                out.append(f'{last_indent}{key}: "{env_vars[key]}"')
            continue
        out.append(line)
        i += 1

    if found_env:
        return "\n".join(out)

    # No top-level env block found -> insert one after the name: line.
    env_block = "env:\n"
    for k, v in env_vars.items():
        env_block += f'  {k}: "{v}"\n'

    if yaml_content.startswith("name:"):
        first_newline = yaml_content.find("\n")
        return (
            yaml_content[: first_newline + 1]
            + env_block
            + yaml_content[first_newline + 1 :]
        )
    return env_block + yaml_content


def deploy(folder: str):
    """
    deploy project
    :param folder:
    :return:
    """

    # Generate auth keys for injection into the workflow env block.
    community_key, sponsor_key = encrypt_info()

    # Read app_name and version from launcher.py for the workflow env block.
    app_name, version = _read_launcher_meta()

    # Create .github folder if it doesn't exist
    github_path = join(getcwd(), '.github')
    if not exists(github_path):
        makedirs(github_path)
    # Create .github/workflows folder if it doesn't exist
    workflows_path = join(github_path, 'workflows')
    if not exists(workflows_path):
        makedirs(workflows_path)
    # Copy greaterwms.yaml from bomiot package to .github/workflows
    # and inject/update the workflow-global env block.
    import bomiot
    bomiot_dir = Path(bomiot.__file__).parent
    source_yaml = bomiot_dir / 'cmd' / 'file' / 'greaterwms.yaml'
    dest_yaml = join(workflows_path, 'greaterwms.yaml')
    if exists(source_yaml):
        with open(str(source_yaml), 'r', encoding='utf-8') as f:
            yaml_content = f.read()
        env_vars = {
            "APP_NAME": app_name,
            "BASE_VERSION": version,
            "COMMUNITY_KEY": community_key,
            "SPONSOR_KEY": sponsor_key,
        }
        yaml_content = _inject_env(yaml_content, env_vars)
        with open(dest_yaml, 'w', encoding='utf-8') as f:
            f.write(yaml_content)

    print(f'Deploy project success')
