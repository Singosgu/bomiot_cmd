import os, sys
from pathlib import Path

def create_project_apps_py(path, app, name):
    with open(path, "w") as f:
        f.write("from django.apps import AppConfig\n")
        f.write("\n")
        f.write("ARGS_MAP = {\n")
        f.write("    'cron': ['year', 'month', 'day', 'week', 'day_of_week', 'hour', 'minute', 'second', 'start_date', 'end_date', 'timezone'],\n")
        f.write("    'interval': ['weeks', 'days', 'hours', 'minutes', 'seconds', 'start_date', 'end_date', 'timezone'],\n")
        f.write("    'date': ['run_date', 'timezone']\n")
        f.write("}\n")
        f.write("\n")
        f.write(f"class {name.capitalize()}Config(AppConfig):\n")
        f.write(f"    name = '{app}.{name}'\n")
        f.write("\n")
        f.write("    def ready(self):\n")
        f.write("        from bomiot.server.core.signal import bomiot_signals, bomiot_data_signals\n")
        f.write("        from bomiot.server.core.models import JobList\n")
        f.write("        import json\n")
        f.write("\n")
        f.write("        try:\n")
        f.write("            JobList.objects.get_or_create(\n")
        f.write(f"                job_id='example_job',\n")
        f.write("                defaults={\n")
        f.write(f"                    'module_name': 'greaterwms.task',\n")
        f.write("                    'func_name': 'example_job',\n")
        f.write("                    'trigger': 'interval',\n")
        f.write("                    'configuration': json.dumps({'minutes': 1}),\n")
        f.write("                    'description': 'Example scheduled task - Executed once every 1 minute',\n")
        f.write("                    'type': True,\n")
        f.write("                }\n")
        f.write("            )\n")
        f.write("        except:\n")
        f.write("            pass\n")
    f.close()
    from bomiot.cmd.file.discovered_apps import main as _discover_apps_main
    _orig_argv = sys.argv
    sys.argv = [sys.argv[0]]
    _discover_apps_main(str(Path(path).parent.parent.parent))
    sys.argv = _orig_argv
