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
        # Inject auth keys into the env section
        yaml_content = yaml_content.replace(
            'env:\n',
            f'env:\n  COMMUNITY_KEY: "{community_key}"\n  SPONSOR_KEY: "{sponsor_key}"\n',
            1,
        )
        with open(dest_yaml, 'w', encoding='utf-8') as f:
            f.write(yaml_content)

    print(f'Deploy project success')
