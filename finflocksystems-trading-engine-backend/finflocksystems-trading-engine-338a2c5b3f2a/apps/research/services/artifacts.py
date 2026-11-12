from abc import ABC, abstractmethod
from pathlib import Path


class ArtifactStore(ABC):
    @abstractmethod
    def write_table(self, key, rows):
        raise NotImplementedError

    @abstractmethod
    def read_table(self, uri):
        raise NotImplementedError


class FilesystemArtifactStore(ArtifactStore):
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        path = (self.root / f"{key}.parquet").resolve()
        if self.root not in path.parents:
            raise ValueError("Artifact key escapes configured root")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_table(self, key, rows):
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = self._path(key)
        temporary = path.with_suffix(".parquet.tmp")
        pq.write_table(pa.Table.from_pylist(list(rows)), temporary, compression="zstd")
        temporary.replace(path)
        return str(path)

    def read_table(self, uri):
        import pyarrow.parquet as pq

        path = Path(uri).resolve()
        if self.root not in path.parents or path.suffix != ".parquet":
            raise ValueError("Artifact URI is outside the configured Parquet store")
        return pq.read_table(path).to_pylist()
