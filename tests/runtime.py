"""A disposable default store for engine tests that only stub Signal operations."""
import tempfile
from contextlib import contextmanager
from pathlib import Path

import engine
from mac_worker import configure_storage


@contextmanager
def isolated_engine():
    previous = dict(vars(engine))
    with tempfile.TemporaryDirectory(prefix="sb-engine-tests-") as directory:
        configure_storage(Path(directory).resolve())
        engine.LOGS_DIR.mkdir()
        try:
            yield
        finally:
            for name, value in previous.items():
                if name.isupper():
                    setattr(engine, name, value)
