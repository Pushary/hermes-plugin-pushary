import json
import re
import unittest
from fnmatch import fnmatch
from pathlib import Path

from pushary_plugin import installation

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIRECTORY = PACKAGE_ROOT / "pushary_plugin"
SHIPPED_SUFFIXES = (".py", ".yaml", ".md", ".js", ".json")
PACKAGE_DATA_LINE = re.compile(r'^"pushary_plugin" = (\[.*\])$', re.MULTILINE)


def shipped_files():
    return sorted(
        path.relative_to(PLUGIN_DIRECTORY).as_posix()
        for path in PLUGIN_DIRECTORY.rglob("*")
        if path.is_file()
        and path.suffix in SHIPPED_SUFFIXES
        and not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(PLUGIN_DIRECTORY).parts)
    )


def package_data_globs():
    match = PACKAGE_DATA_LINE.search((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError("pyproject.toml no longer lists package-data for pushary_plugin")
    return json.loads(match.group(1))


def is_in_the_wheel(relative_path, globs):
    is_package_module = "/" not in relative_path and relative_path.endswith(".py")
    return is_package_module or any(fnmatch(relative_path, glob) for glob in globs)


class PackagingTest(unittest.TestCase):
    def test_every_file_the_plugin_ships_is_in_the_wheel(self):
        globs = package_data_globs()
        missing = [path for path in shipped_files() if not is_in_the_wheel(path, globs)]
        self.assertEqual(missing, [])

    def test_the_desktop_manifest_names_the_plugin_version(self):
        manifest = json.loads((PLUGIN_DIRECTORY / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], installation.plugin_version())


if __name__ == "__main__":
    unittest.main()
