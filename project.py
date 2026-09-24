from os.path import join, exists
from os import makedirs, getcwd
import os
import sys
import shutil
from pathlib import Path
from .init import create_file
import importlib.metadata
from configparser import ConfigParser
from .copyfile import copy_files
import logging


def project(folder: str):
    """
    project workspace
    :param folder: Project folder name
    :return: None
    """
    if len(sys.argv) < 3:
        print('Please enter your project name')
        return
    
    project_name = sys.argv[2]
    
    # Check if project name is reserved
    if project_name.lower() == 'greaterwms':
        print('Project name "greaterwms" is reserved and cannot be used')
        return
    
    project_path = join(getcwd(), project_name)
    
    if exists(project_path):
        print('Project directory already exists')
        return
    
    # Check if project name conflicts with installed packages
    try:
        installed_packages = [dist.metadata['Name'] for dist in importlib.metadata.distributions()]
        if project_name in installed_packages:
            print('Project directory already exists')
            return
    except Exception as e:
        logging.warning(f"Could not check installed packages: {e}")
    
    try:
        makedirs(project_path, exist_ok=True)
        current_path = Path(__file__).resolve()
        file_path = join(current_path.parent, 'file')

        # Write init file
        init_file_path = join(project_path, '__init__.py')
        with open(init_file_path, "w", encoding='utf-8') as f:
            pass

        # Copy essential files
        essential_files = [
            'bomiotconf.ini'
        ]
        
        for file_name in essential_files:
            src_path = join(file_path, file_name)
            dst_path = join(project_path, file_name)
            
            if exists(src_path):
                shutil.copy2(src_path, dst_path)
            else:
                logging.warning(f"Essential file not found: {src_path}")

        # Create additional files via create_file function
        create_file(str(project_name))

        # Copy additional directories
        copy_files(join(current_path.parent.parent, 'templates'), join(project_path, 'templates'))

        print(f'Initialized project workspace {project_name}')
        
    except OSError as e:
        print(f"Error creating project directory: {e}")
        return
    except Exception as e:
        print(f"Unexpected error during project initialization: {e}")
        return