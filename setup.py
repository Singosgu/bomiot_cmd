"""Setup for the Nuitka-compiled bomiot_cmd library.

Packages the prebuilt extension module (.pyd/.so) produced by:
    python -m nuitka --mode=module --include-package=bomiot_cmd \
        --output-dir=build --no-pyi-file bomiot_cmd

The compiled .pyd is shipped as a top-level extension module so that
`import bomiot_cmd` works after `pip install`.
"""

import os
import glob
import shutil

from setuptools import setup, Extension
from setuptools.command.build_ext import build_ext


HERE = os.path.dirname(os.path.abspath(__file__))
COMPILED = (
    glob.glob(os.path.join(HERE, "build", "bomiot_cmd*.pyd"))
    + glob.glob(os.path.join(HERE, "build", "bomiot_cmd*.so"))
)

if not COMPILED:
    raise SystemExit(
        "No compiled bomiot_cmd extension found in build/. "
        "Run Nuitka first:\n"
        "  python -m nuitka --mode=module --include-package=bomiot_cmd "
        "--output-dir=build --no-pyi-file bomiot_cmd"
    )


class build_prebuilt_ext(build_ext):
    """Copy the prebuilt Nuitka extension into the wheel's build_lib so it
    lands at the wheel root (importable as a top-level module)."""

    def build_extension(self, ext):
        dest = self.get_ext_fullpath(ext.name)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(ext.sources[0], dest)
        # distutils normally writes a .pyi stub; skip it for prebuilt files.


setup(
    name="bomiot_cmd",
    version="0.1.0",
    description="Bomiot CMD library",
    ext_modules=[Extension("bomiot_cmd", sources=[COMPILED[0]])],
    cmdclass={"build_ext": build_prebuilt_ext},
    python_requires=">=3.10",
    zip_safe=False,
)
