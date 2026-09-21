"""A blank DATA_DIR (the .env.example default) must resolve to the app's data folder in
BOTH stores, never to a database at the filesystem root."""
import importlib
import os


def test_blank_data_dir_means_the_default_for_db_and_calls(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "")
    import eo_db
    import store
    db = importlib.reload(eo_db)
    st = importlib.reload(store)
    try:
        app_dir = os.path.dirname(os.path.abspath(db.__file__))
        assert db._DATA_DIR == os.path.join(app_dir, "data")
        assert os.path.dirname(db._DB_PATH) == db._DATA_DIR
        assert st.DATA_DIR == os.path.join(app_dir, "data")
    finally:
        # restore the test-isolated DATA_DIR the conftest set, for every later test
        monkeypatch.undo()
        importlib.reload(eo_db)
        importlib.reload(store)
