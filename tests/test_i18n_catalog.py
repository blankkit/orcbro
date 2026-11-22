"""Every literal English message in the bridge has a German translation with the same placeholders."""
import ast
import string
import unittest
from pathlib import Path

from bridge.i18n_de import DE

BRIDGE = Path(__file__).resolve().parent.parent / "bridge"


def messages() -> dict[str, str]:
    found = {}
    for path in sorted(BRIDGE.glob("*.py")):
        if path.name == "i18n_de.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("m", "t")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, f"{path.name}:{node.lineno}")
    return found


def fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class CatalogTests(unittest.TestCase):
    def test_every_message_is_translated(self):
        missing = {text: where for text, where in messages().items() if text not in DE}
        self.assertEqual(missing, {})

    def test_placeholders_match(self):
        for english, german in DE.items():
            with self.subTest(english=english):
                self.assertEqual(fields(english), fields(german))
                self.assertEqual(english.count("%"), german.count("%"))


if __name__ == "__main__":
    unittest.main()
