from os.path import join, exists
from os import makedirs, getcwd
import os
import sys
from pathlib import Path
from configparser import ConfigParser
from .copyfile import copy_files
from .changeapps import create_project_apps_py


def new_api(folder: str):
    """
    new app for project
    :param folder:
    :return:
    """
    if len(sys.argv) < 3:
        print('Please enter your app name')
    else:
        if sys.argv[2] == 'bomiot':
            print('Invalid app name. Please enter a valid app name.')
        else:
            current_path = Path(__file__).resolve()
            project_name = 'greaterwms'
            project_path = join(getcwd(), project_name)
            project_config = ConfigParser()
            project_config.read(join(project_path, 'bomiotconf.ini'), encoding='utf-8')
            if project_config.get('mode', 'name') == 'project':
                app_path = join(project_path, sys.argv[2])
                if exists(app_path):
                    print('App directory already exists')
                else:
                    makedirs(app_path)

                    copy_files(join(current_path.parent, 'newapi'), app_path)

                    apps_path = join(app_path, 'apps.py')
                    os.remove(apps_path)
                    create_project_apps_py(apps_path, project_name, sys.argv[2])

                    filter_path = join(app_path, 'filter.py')

                    app_name = sys.argv[2]

                    import re

                    def replace_example_in_file(file_path, app_name, is_filter_file=False, is_serializers_file=False, is_urls_file=False, is_views_file=False):
                        if not exists(file_path):
                            return
                            
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        if is_filter_file:
                            content = content.replace(
                                'from bomiot.server.core import models',
                                f'from {project_name}.{app_name} import models'
                            )
                        
                        if is_serializers_file:
                            content = content.replace(
                                'from bomiot.server.core import models',
                                f'from {project_name}.{app_name} import models'
                            )
                        
                        if is_urls_file:
                            content = content.replace(
                                'from bomiot.server.function import example',
                                f'from {project_name}.{app_name} import views'
                            )

                        if is_views_file:
                            content = content.replace(
                                'from bomiot.server.core import models, serializers, filter',
                                f'from {project_name}.{app_name} import models, serializers, filter'
                            )

                        capitalized_app = app_name.capitalize()
                        content = content.replace('Example', capitalized_app)

                        # Replace 'example' (lowercase) with app_name (lowercase)
                        content = content.replace('example', app_name.lower())
                        
                        # For urls.py, replace ', app_name' with ', views' for module references
                        if is_urls_file:
                            content = content.replace(f', {app_name}', ', views')
                            content = content.replace(f', {app_name.lower()}', ', views')
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(content)
                    
                    # Process all relevant files
                    for file_name in ['filter.py', 'models.py', 'serializers.py', 'urls.py', 'views.py']:
                        file_path = join(app_path, file_name)
                        is_filter = (file_name == 'filter.py')
                        is_serializers = (file_name == 'serializers.py')
                        is_urls = (file_name == 'urls.py')
                        is_views = (file_name == 'views.py')
                        replace_example_in_file(file_path, app_name, is_filter_file=is_filter, is_serializers_file=is_serializers, is_urls_file=is_urls, is_views_file=is_views)

                    models_path = join(app_path, 'models.py')

                    serializers_path = join(app_path, 'serializers.py')

                    urls_path = join(app_path, 'urls.py')

                    views_path = join(app_path, 'views.py')

                    print(f'Create APP success {sys.argv[2]}')
            else:
                print('Invalid project mode. Please create a new project or switch to project mode.')
