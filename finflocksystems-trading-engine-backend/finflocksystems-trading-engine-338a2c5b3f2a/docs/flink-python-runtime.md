# Flink worker Python runtime

The Backend and the external Flink TaskManager do not share a filesystem. The
Backend therefore ships a self-contained worker runtime in every PyFlink job
graph instead of referring to `/opt/pyflink-venv` on the Backend container.

The Docker build creates `/opt/flink-python-runtime/flink-python-runtime.zip`
from the pinned `python-build-standalone` Linux `x86_64` GNU CPython 3.11.10
artifact. Its SHA-256 is verified before extraction. `apache-flink==1.20.1` and
its worker dependencies are installed into the standalone distribution itself,
not into a virtual environment whose interpreter points outside the archive.
The build rejects absolute and broken symlinks, creates a reproducible ZIP, then
extracts and executes the packaged interpreter as a relocation check.

PyFlink 1.20 extracts the ZIP under the configured target directory on each
TaskManager. The defaults therefore match exactly:

```dotenv
FLINK_PYTHON_RUNTIME_MODE=archive
FLINK_PYTHON_ARCHIVE=/opt/flink-python-runtime/flink-python-runtime.zip
FLINK_PYTHON_ARCHIVE_TARGET=pyenv
FLINK_PYTHON_EXECUTABLE=pyenv/bin/python
```

The supported reproducible build entry point is the Backend image build:

```bash
docker build --progress=plain -t trading-engine-backend:flink-python-runtime .
```

The pinned source inputs are declared as Docker build arguments. A deliberate
runtime upgrade must update both the URL and checksum together:

```text
URL: https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
SHA-256: 03f15e19e2452641b6375b59ba094ff6cf2fc118315d24a6ca63ce60e4d4a6e0
```

The external TaskManager must run Linux `amd64` with a GNU libc-compatible
userspace. It does not need Python or the Backend filesystem. If a deployment
instead guarantees an exact TaskManager-local interpreter, operators may select
`taskmanager-path` mode and an absolute path, but that guarantee lives outside
this repository and must be established from the TaskManager image.
