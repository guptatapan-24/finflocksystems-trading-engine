#!/bin/sh
set -eu

# Build the relocatable PyFlink worker ZIP used by remote Linux amd64
# TaskManagers. This script is intentionally run during the Docker build, never
# during Backend startup.

: "${PYTHON_RUNTIME_URL:?PYTHON_RUNTIME_URL is required}"
: "${PYTHON_RUNTIME_SHA256:?PYTHON_RUNTIME_SHA256 is required}"

OUTPUT_PATH="${1:-/opt/flink-python-runtime/flink-python-runtime.zip}"
BUILD_ROOT="/tmp/flink-python-runtime-build"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1729036800}"
RUNTIME_TARBALL="${BUILD_ROOT}/python-runtime.tar.gz"
RUNTIME_ROOT="${BUILD_ROOT}/runtime"
VERIFY_ROOT="${BUILD_ROOT}/verification"

rm -rf "${BUILD_ROOT}"
mkdir -p "${RUNTIME_ROOT}" "$(dirname "${OUTPUT_PATH}")"

curl --fail --location --silent --show-error \
  --output "${RUNTIME_TARBALL}" "${PYTHON_RUNTIME_URL}"
printf '%s  %s\n' "${PYTHON_RUNTIME_SHA256}" "${RUNTIME_TARBALL}" | sha256sum -c -
tar -xzf "${RUNTIME_TARBALL}" -C "${RUNTIME_ROOT}" --strip-components=1

"${RUNTIME_ROOT}/bin/python3" -m pip install \
  --disable-pip-version-check \
  --no-cache-dir \
  "apache-flink==1.20.1"
"${RUNTIME_ROOT}/bin/python3" -c \
  "import platform; from pyflink.version import __version__; assert platform.machine() == 'x86_64'; assert __version__ == '1.20.1'"

# No TaskManager may be asked to follow a Backend/build-host-only symlink.
if find "${RUNTIME_ROOT}" -type l -lname '/*' -print | grep -q .; then
  echo "Portable Python runtime contains an absolute symlink" >&2
  exit 1
fi
if find "${RUNTIME_ROOT}" -xtype l -print | grep -q .; then
  echo "Portable Python runtime contains a broken symlink" >&2
  exit 1
fi

find "${RUNTIME_ROOT}" -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +
rm -f "${OUTPUT_PATH}"
(
  cd "${RUNTIME_ROOT}"
  find . -mindepth 1 -printf '%P\n' | LC_ALL=C sort | zip -q -X -y "${OUTPUT_PATH}" -@
)

test -r "${OUTPUT_PATH}"
unzip -Z1 "${OUTPUT_PATH}" | grep -qx 'bin/python'
mkdir -p "${VERIFY_ROOT}"
unzip -q "${OUTPUT_PATH}" -d "${VERIFY_ROOT}"
test -x "${VERIFY_ROOT}/bin/python"
"${VERIFY_ROOT}/bin/python" -c \
  "import platform; from pyflink.version import __version__; assert platform.machine() == 'x86_64'; assert __version__ == '1.20.1'"
if find "${VERIFY_ROOT}" -xtype l -print | grep -q .; then
  echo "Packaged Python runtime contains a broken symlink after extraction" >&2
  exit 1
fi

echo "Built verified PyFlink worker archive at ${OUTPUT_PATH}"
