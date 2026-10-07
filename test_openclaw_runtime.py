"""The OpenClaw runtime must never capture the host venv or private plan data."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("runtime_builder", ROOT / "scripts/build_openclaw_runtime.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class RuntimeTests(unittest.TestCase):
    def test_private_data_and_external_venv_are_excluded_and_builds_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("package.json", "openclaw.plugin.json", "src/index.js", "skill/SKILL.md", "LICENSE", "README.md"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            (root / ".venv/bin").mkdir(parents=True)
            (root / ".venv/bin/python").symlink_to("/usr/bin/python3")
            (root / "data").mkdir()
            (root / "data/private.json").write_text('{"secret":"DO_NOT_PACKAGE"}')
            first = builder.build_runtime(root)
            self.assertEqual(first, builder.build_runtime(root))
            self.assertEqual({str(p.relative_to(first)) for p in first.rglob("*") if p.is_file()},
                             {"package.json", "openclaw.plugin.json", "src/index.js", "skill/SKILL.md", "LICENSE", "README.md"})
            self.assertNotIn("scripts", json.loads((first / "package.json").read_text()))
            original = (first / "src/index.js").read_bytes()
            with (root / "src/index.js").open("a") as f:
                f.write("\n// changed\n")
            second = builder.build_runtime(root)
            self.assertNotEqual(first, second)
            self.assertEqual((first / "src/index.js").read_bytes(), original)
            (second / "unexpected.txt").write_text("unsafe extra")
            with self.assertRaises(ValueError):
                builder.build_runtime(root)


if __name__ == "__main__":
    unittest.main()
