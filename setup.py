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
import sys
# Platform-aware: only pick the extension matching the current platform.
# A stale .pyd committed from a Windows build must not be packaged into a
# Linux/macOS wheel (auditwheel would find no ELF and fail).
_compiled_pat = "bomiot_cmd*.pyd" if sys.platform == "win32" else "bomiot_cmd*.so"
COMPILED = glob.glob(os.path.join(HERE, "build", _compiled_pat))

if not COMPILED:
    raise SystemExit(
        "No compiled bomiot_cmd extension found in build/. "
        "Run Nuitka first:\n"
        "  python -m nuitka --mode=module --include-package=bomiot_cmd "
        "--output-dir=build --no-pyi-file bomiot_cmd"
    )


class build_prebuilt_ext(build_ext):
    """Copy the prebuilt Nuitka extension into the wheel's build_lib so it
    lands at the wheel root (importable as a top-level module).

    We override run() directly instead of build_extension(): the base
    run()/build_extensions() assumes C/C++ sources and may skip an ext
    whose sources list a .so/.pyd, leaving the wheel without the binary.
    """

    def run(self):
        self.mkpath(self.build_lib)
        for ext in self.extensions:
            dest = self.get_ext_fullpath(ext.name)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            shutil.copy2(ext.sources[0], dest)
            print(f"[build_prebuilt_ext] {ext.sources[0]} -> {dest}", flush=True)

    def get_outputs(self):
        # Tell bdist_wheel exactly which files we produced.
        return [self.get_ext_fullpath(ext.name) for ext in self.extensions]


setup(
    name="bomiot_cmd",
    version="0.1.8",
    description="Bomiot CMD library",
    ext_modules=[Extension("bomiot_cmd", sources=[COMPILED[0]])],
    cmdclass={"build_ext": build_prebuilt_ext},
    python_requires=">=3.10",
    zip_safe=False,
)
