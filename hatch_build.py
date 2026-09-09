"""Include prebuilt browser assets when producing a web-capable wheel."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class FrontendBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        frontend = Path(self.root) / "web" / "dist"
        if self.target_name == "wheel" and (frontend / "index.html").is_file():
            build_data["force_include"][str(frontend)] = "epiq/_frontend"
