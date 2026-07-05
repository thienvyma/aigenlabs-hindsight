from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import TYPE_CHECKING
from pathlib import Path
from urllib.request import urlopen

if TYPE_CHECKING:
    from pg0 import Pg0

logger = logging.getLogger(__name__)

DEFAULT_USERNAME = "hindsight"
DEFAULT_PASSWORD = "hindsight"
DEFAULT_DATABASE = "hindsight"
DEFAULT_PGVECTOR_VERSION = "0.8.1"
PGVECTOR_REPAIR_ENV = "HINDSIGHT_API_PG0_REPAIR_PGVECTOR"
PGVECTOR_VERSION_ENV = "HINDSIGHT_API_PG0_PGVECTOR_VERSION"
PGVECTOR_PROBE_TIMEOUT_SECONDS = 20
PGVECTOR_BUILD_TIMEOUT_SECONDS = 240
PGVECTOR_DOWNLOAD_TIMEOUT_SECONDS = 60


class EmbeddedPostgres:
    """Manages an embedded PostgreSQL server instance using pg0-embedded."""

    def __init__(
        self,
        port: int | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        database: str = DEFAULT_DATABASE,
        name: str = "hindsight",
        config: dict[str, str] | None = None,
        **kwargs,
    ):
        self.port = port  # None means pg0 will auto-assign
        self.username = username
        self.password = password
        self.database = database
        self.name = name
        # Extra postgresql.conf settings forwarded to Pg0 (e.g. ``max_connections``).
        # Useful when tests spawn many xdist workers that each open a pool against
        # the same pg0 instance — the postgres default of 100 max_connections is
        # easy to exhaust under that fan-out.
        self.config = config
        self._pg0: Pg0 | None = None

    def _get_pg0(self) -> Pg0:
        if self._pg0 is None:
            try:
                from pg0 import Pg0
            except ImportError:
                raise ImportError(
                    "pg0-embedded is required for embedded PostgreSQL. "
                    "Install it with: pip install 'hindsight-api-slim[embedded-db]'"
                )
            kwargs = {
                "name": self.name,
                "username": self.username,
                "password": self.password,
                "database": self.database,
            }
            # Only set port if explicitly specified
            if self.port is not None:
                kwargs["port"] = self.port
            if self.config is not None:
                kwargs["config"] = self.config
            self._pg0 = Pg0(**kwargs)
        return self._pg0

    async def start(self, max_retries: int = 5, retry_delay: float = 4.0) -> str:
        """Start the PostgreSQL server with retry logic."""
        port_info = f"port={self.port}" if self.port else "port=auto"
        logger.info(f"Starting embedded PostgreSQL (name={self.name}, {port_info})...")

        pg0 = self._get_pg0()
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, pg0.start)
                # Get URI from pg0 (includes auto-assigned port)
                uri = info.uri
                logger.info(f"PostgreSQL started: {uri}")
                await loop.run_in_executor(
                    None,
                    _ensure_pg0_pgvector_loadable,
                    uri,
                    info.version,
                )
                return uri
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    delay = retry_delay * (2 ** (attempt - 1))
                    logger.debug(f"pg0 start attempt {attempt}/{max_retries} failed: {last_error}")
                    logger.debug(f"Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.debug(f"pg0 start attempt {attempt}/{max_retries} failed: {last_error}")

        raise RuntimeError(
            f"Failed to start embedded PostgreSQL after {max_retries} attempts. Last error: {last_error}"
        )

    async def stop(self) -> None:
        """Stop the PostgreSQL server."""
        pg0 = self._get_pg0()
        logger.info(f"Stopping embedded PostgreSQL (name: {self.name})...")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pg0.stop)
            logger.info("Embedded PostgreSQL stopped")
        except Exception as e:
            if "not running" in str(e).lower():
                return
            raise RuntimeError(f"Failed to stop PostgreSQL: {e}")

    async def get_uri(self) -> str:
        """Get the connection URI for the PostgreSQL server."""
        pg0 = self._get_pg0()
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, pg0.info)
        return info.uri

    async def is_running(self) -> bool:
        """Check if the PostgreSQL server is currently running."""
        try:
            pg0 = self._get_pg0()
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, pg0.info)
            return info is not None and info.running
        except Exception:
            return False

    async def ensure_running(self) -> str:
        """Ensure the PostgreSQL server is running, starting it if needed."""
        if await self.is_running():
            return await self.get_uri()
        return await self.start()


_default_instance: EmbeddedPostgres | None = None


def get_embedded_postgres() -> EmbeddedPostgres:
    """Get or create the default EmbeddedPostgres instance."""
    global _default_instance
    if _default_instance is None:
        _default_instance = EmbeddedPostgres()
    return _default_instance


async def start_embedded_postgres() -> str:
    """Quick start function for embedded PostgreSQL."""
    return await get_embedded_postgres().ensure_running()


async def stop_embedded_postgres() -> None:
    """Stop the default embedded PostgreSQL instance."""
    global _default_instance
    if _default_instance:
        await _default_instance.stop()


def parse_pg0_url(db_url: str) -> tuple[bool, str | None, int | None]:
    """
    Parse a database URL and check if it's a pg0:// embedded database URL.

    Supports:
    - "pg0" -> default instance "hindsight"
    - "pg0://instance-name" -> named instance
    - "pg0://instance-name:port" -> named instance with explicit port
    - Any other URL (e.g., postgresql://) -> not a pg0 URL

    Args:
        db_url: The database URL to parse

    Returns:
        Tuple of (is_pg0, instance_name, port)
        - is_pg0: True if this is a pg0 URL
        - instance_name: The instance name (or None if not pg0)
        - port: The explicit port (or None for auto-assign)
    """
    if db_url == "pg0":
        return True, "hindsight", None

    if db_url.startswith("pg0://"):
        url_part = db_url[6:]  # Remove "pg0://"
        if ":" in url_part:
            instance_name, port_str = url_part.rsplit(":", 1)
            return True, instance_name or "hindsight", int(port_str)
        else:
            return True, url_part or "hindsight", None

    return False, None, None


def _pg0_installation_root(version: str | None) -> Path | None:
    version_text = str(version or "").strip()
    if not version_text:
        return None
    return Path.home() / ".pg0" / "installation" / version_text


def _pgvector_repair_enabled() -> bool:
    return os.getenv(PGVECTOR_REPAIR_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _ensure_pg0_pgvector_loadable(uri: str | None, version: str | None) -> None:
    """Ensure pg0's bundled pgvector can load on this host.

    Some pg0 Linux wheels bundle a PostgreSQL/pgvector binary built against a
    newer glibc than Ubuntu 22.04 provides. PostgreSQL itself starts, then
    migrations fail at ``CREATE EXTENSION vector``. Rebuilding pgvector against
    pg0's own ``pg_config`` keeps the embedded database self-contained while
    producing a ``vector.so`` compatible with the user's machine.
    """

    if not uri:
        return
    install_root = _pg0_installation_root(version)
    if install_root is None:
        return
    if not (install_root / "bin" / _executable_name("psql")).exists():
        return

    try:
        _probe_pgvector_extension(install_root, uri)
        return
    except Exception as exc:
        if not _pgvector_repair_enabled():
            raise
        logger.warning(
            "pg0 pgvector extension failed to load; rebuilding local pgvector "
            "for this host (%s)",
            exc.__class__.__name__,
        )

    _rebuild_pgvector_for_pg0(install_root)
    _probe_pgvector_extension(install_root, uri)
    logger.info("pg0 pgvector extension verified after local rebuild")


def _executable_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _probe_pgvector_extension(install_root: Path, uri: str) -> None:
    psql = install_root / "bin" / _executable_name("psql")
    result = subprocess.run(
        [
            str(psql),
            uri,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE EXTENSION IF NOT EXISTS vector",
        ],
        capture_output=True,
        text=True,
        timeout=PGVECTOR_PROBE_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or "CREATE EXTENSION vector failed")


def _rebuild_pgvector_for_pg0(install_root: Path) -> None:
    pg_config = install_root / "bin" / _executable_name("pg_config")
    if not pg_config.exists():
        raise RuntimeError(f"pg0 pg_config not found: {pg_config}")
    for tool in ("make", "cc"):
        if shutil.which(tool) is None and (tool != "cc" or shutil.which("gcc") is None):
            raise RuntimeError(
                "pgvector rebuild requires build tools; install make and a C compiler"
            )

    version = os.getenv(PGVECTOR_VERSION_ENV, DEFAULT_PGVECTOR_VERSION).strip()
    if not version:
        version = DEFAULT_PGVECTOR_VERSION
    with tempfile.TemporaryDirectory(prefix="hindsight-pgvector-") as temp_dir:
        temp_path = Path(temp_dir)
        source_dir = _download_pgvector_source(version, temp_path)
        env = {**os.environ, "PG_CONFIG": str(pg_config)}
        _run_make(source_dir, "clean", env=env, check=False)
        _run_make(source_dir, env=env)
        _run_make(source_dir, "install", env=env)


def _download_pgvector_source(version: str, temp_path: Path) -> Path:
    url = f"https://github.com/pgvector/pgvector/archive/refs/tags/v{version}.tar.gz"
    with urlopen(url, timeout=PGVECTOR_DOWNLOAD_TIMEOUT_SECONDS) as response:
        payload = response.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        _safe_extract_tar(archive, temp_path)
    source_dir = temp_path / f"pgvector-{version}"
    if not source_dir.is_dir():
        raise RuntimeError(f"pgvector source archive did not contain {source_dir.name}")
    return source_dir


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if target != destination and destination not in target.parents:
            raise RuntimeError(f"unsafe path in pgvector source archive: {member.name}")
    archive.extractall(destination)


def _run_make(
    source_dir: Path,
    *args: str,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["make", *args],
        cwd=source_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=PGVECTOR_BUILD_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"make {' '.join(args) or 'build'} failed")
    return result


async def resolve_database_url(db_url: str) -> str:
    """
    Resolve a database URL, handling pg0:// embedded database URLs.

    If the URL is a pg0:// URL, starts the embedded PostgreSQL and returns
    the actual postgresql:// connection URL. Otherwise, returns the URL unchanged.

    Args:
        db_url: Database URL (pg0://, pg0, or postgresql://)

    Returns:
        The resolved postgresql:// connection URL
    """
    is_pg0, instance_name, port = parse_pg0_url(db_url)
    if is_pg0:
        pg0 = EmbeddedPostgres(name=instance_name, port=port)
        return await pg0.ensure_running()
    return db_url
