"""External fixture ownership checks. No real source, credential or network."""

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from tests.automatic_gui_setup import create, add_changes, owned


class GuiHelperTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(create()["directory"])
        self.addCleanup(shutil.rmtree, self.root)

    def test_generated_proposals_preserve_pause_and_never_create_calls(self):
        result = add_changes(self.root)
        self.assertTrue(result["generated_only"])
        self.assertTrue(result["paused"])
        self.assertEqual(result["model_requests"], 0)
        with closing(sqlite3.connect(self.root / "application.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT kind,status FROM task_changes ORDER BY kind").fetchall(),
                [("cancelled","pending"),("completed","pending"),("details","pending")])
            self.assertEqual({s for _,s,_ in result["tasks"]}, {"needs_confirmation"})

    def test_repeated_generation_does_not_reset_user_resolution(self):
        add_changes(self.root)
        with closing(sqlite3.connect(self.root / "application.sqlite3")) as db:
            db.execute("UPDATE task_changes SET status='kept' WHERE kind='details'")
            db.execute("UPDATE tasks SET status='completed',revision=2 WHERE task_id='historical-task'")
            db.commit()
        add_changes(self.root)
        with closing(sqlite3.connect(self.root / "application.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM task_changes").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT status FROM task_changes WHERE kind='details'").fetchone()[0],"kept")
            self.assertEqual(db.execute("SELECT status,revision FROM tasks WHERE task_id='historical-task'").fetchone(),("completed",2))

    def test_missing_owner_refuses_mutation(self):
        with closing(sqlite3.connect(self.root / "application.sqlite3")) as db:
            db.execute("DELETE FROM settings WHERE key='gui_fixture_owner'"); db.commit()
        with self.assertRaises(ValueError):
            add_changes(self.root)

    def test_linked_database_refuses_mutation(self):
        with tempfile.TemporaryDirectory(prefix="wechat-today-helper-check-") as elsewhere:
            backup = Path(elsewhere) / "database"
            database = self.root / "application.sqlite3"
            database.rename(backup)
            database.symlink_to(backup)
            with self.assertRaises(ValueError):
                owned(self.root)


if __name__ == "__main__":
    unittest.main()
