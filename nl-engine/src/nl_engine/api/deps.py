from collections.abc import Generator

from nl_engine.persistence.db import FileStore, get_file_store


def get_db() -> Generator[FileStore, None, None]:
    """Yield the global FileStore instance.

    Name kept as ``get_db`` to minimize churn in endpoint signatures.
    """
    yield get_file_store()
