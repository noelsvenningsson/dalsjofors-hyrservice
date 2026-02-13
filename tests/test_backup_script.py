import unittest
from pathlib import Path


class BackupScriptTest(unittest.TestCase):
    def test_backup_script_exists(self) -> None:
        script_path = Path(__file__).resolve().parent.parent / "scripts" / "backup_db.sh"
        self.assertTrue(script_path.is_file(), f"Missing script: {script_path}")

    def test_readme_documents_backup_usage(self) -> None:
        readme_path = Path(__file__).resolve().parent.parent / "README.md"
        content = readme_path.read_text(encoding="utf-8")
        self.assertIn("./scripts/backup_db.sh", content)
        self.assertIn("backups/", content)


if __name__ == "__main__":
    unittest.main()
