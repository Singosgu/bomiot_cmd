from os.path import join, exists
from os import makedirs, getcwd, rename
import shutil
from pathlib import Path
from bomiot_token import encrypt_info
from bomiot_cmd.init import create_file
from bomiot_cmd.build import check_sponsor
import sys


def deploy(folder: str):
    """
    deploy project
    :param folder:
    :return:
    """

    # Verify sponsor status before deploying; abort if expired or check fails.
    community_key, sponsor_key = encrypt_info()
    payload = {
        "COMMUNITY_KEY": community_key,
        "SPONSOR_KEY": sponsor_key,
    }
    if not check_sponsor(payload):
        return

    # Create .github folder if it doesn't exist
    github_path = join(getcwd(), '.github')
    if not exists(github_path):
        makedirs(github_path)
    # Create .github/workflows folder if it doesn't exist
    workflows_path = join(github_path, 'workflows')
    if not exists(workflows_path):
        makedirs(workflows_path)
    # Copy greaterwms.yaml from bomiot package to .github/workflows
    # and inject COMMUNITY_KEY / SPONSOR_KEY into the env section.
    import bomiot
    bomiot_dir = Path(bomiot.__file__).parent
    source_yaml = bomiot_dir / 'cmd' / 'file' / 'greaterwms.yaml'
    dest_yaml = join(workflows_path, 'greaterwms.yaml')
    if exists(source_yaml):
        with open(str(source_yaml), 'r', encoding='utf-8') as f:
            yaml_content = f.read()
        # Inject auth keys as a top-level (workflow-global) env block,
        # placed right after the name: line so all jobs share the keys.
        global_env = (
            f"\nenv:\n"
            f"  COMMUNITY_KEY: \"{community_key}\"\n"
            f"  SPONSOR_KEY: \"{sponsor_key}\"\n"
        )
        if yaml_content.startswith("name:"):
            first_newline = yaml_content.find("\n")
            yaml_content = (
                yaml_content[: first_newline + 1]
                + global_env
                + yaml_content[first_newline + 1 :]
            )
        else:
            yaml_content = global_env.lstrip("\n") + yaml_content
        with open(dest_yaml, 'w', encoding='utf-8') as f:
            f.write(yaml_content)

    print(f'Deploy project success')
