"""docs/reference.md lists every route the bridge serves and every environment variable it reads."""
import re
import tempfile
import unittest
from pathlib import Path

from bridge.config import Config
from bridge.server import PRINTER_ROUTES, create_app
from helpers import FILAMENT_IDS, MODELS, PROFILES

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")


def documented_routes() -> dict[str, set[str]]:
    """Methods per path from the reference's route tables (`| GET/POST | `/api/jobs` | …`)."""
    found: dict[str, set[str]] = {}
    for methods, path in re.findall(r"^\| ([A-Z /]+) \| `([^`?]+)[`?]", REFERENCE, re.M):
        found.setdefault(path, set()).update(m.strip() for m in methods.split("/") if m.strip())
    return found


class ReferenceTests(unittest.TestCase):
    def test_every_route_is_documented(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Config(data_dir=Path(tmp), profiles_dir=PROFILES, filament_ids=FILAMENT_IDS,
                                    models_dir=MODELS), start_background=False)
        printer_paths = {path for _method, path, _handler in PRINTER_ROUTES}
        table = documented_routes()
        missing = []
        for route in app.router.routes():
            if route.method == "HEAD":
                continue
            path = re.sub(r"\{\w+\}", "{id}", route.resource.canonical)
            prefix = "/api/printers/{id}"
            if path.startswith(prefix + "/") and path[len(prefix):] in printer_paths:
                path = "/api" + path[len(prefix):]  # Documented once, for the default printer.
            if path.startswith("/api/") or path == "/healthz":
                if route.method not in table.get(path, set()):
                    missing.append(f"{route.method} {path}")
            elif f"`{route.method} {path}" not in REFERENCE:
                missing.append(f"{route.method} {path}")
        self.assertEqual(missing, [])

    def test_every_environment_variable_is_documented(self):
        sources = [(ROOT / "bridge" / name).read_text(encoding="utf-8") for name in ("config.py", "announce.py")]
        variables = {name for source in sources for name in re.findall(r'"((?:BRIDGE|BAMBU|ORCA|HMS|APNS)_[A-Z_]+|PORT)"',
                                                                      source)}
        self.assertIn("BRIDGE_TRUSTED_PROXIES", variables)
        self.assertEqual(sorted(v for v in variables if f"`{v}`" not in REFERENCE), [])


if __name__ == "__main__":
    unittest.main()
