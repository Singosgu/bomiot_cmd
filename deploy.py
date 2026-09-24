from os.path import join, exists
from os import makedirs, getcwd, rename
import shutil
from pathlib import Path
from .init import create_file
import sys


def deploy(folder: str):
    """
    deploy project
    :param folder:
    :return:
    """

    # Create .github folder if it doesn't exist
    github_path = join(getcwd(), '.github')
    if not exists(github_path):
        makedirs(github_path)
    # Create .github/workflows folder if it doesn't exist
    workflows_path = join(github_path, 'workflows')
    if not exists(workflows_path):
        makedirs(workflows_path)
    # Copy greaterwms.yaml to .github/workflows
    current_dir = Path(__file__).parent
    source_yaml = current_dir / 'file' / 'greaterwms.yaml'
    dest_yaml = join(workflows_path, 'greaterwms.yaml')
    if exists(source_yaml) and not exists(dest_yaml):
        shutil.copy2(str(source_yaml), dest_yaml)

    print(f'Deploy project success')
