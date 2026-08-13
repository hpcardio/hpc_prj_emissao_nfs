from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from nfs_fortaleza.config import PROJECT_ROOT, normalize_postgres_url


DEFAULT_SPU_DOWNLOADS_DIR = PROJECT_ROOT / "downloads" / "spu"
DEFAULT_SPU_STORAGE_STATE_PATH = DEFAULT_SPU_DOWNLOADS_DIR / "auth_state.json"
DEFAULT_SPU_BROWSER_PROFILE_DIR = DEFAULT_SPU_DOWNLOADS_DIR / "browser_profile"


@dataclass(frozen=True)
class SpuSettings:
    portal_url: str
    materializer_url: str
    login: str
    password: str
    database_url: str
    postgres_schema: str
    storage_state_path: Path
    browser_headless: bool
    browser_executable_path: str | None
    browser_profile_dir: Path | None
    auto_renew_session: bool
    auth_timeout_seconds: float
    page_timeout_seconds: float
    download_delay_seconds: float
    process_batch_size: int
    pdf_batch_size: int

    @property
    def portal_origin(self) -> str:
        return _origin(self.portal_url)

    @property
    def materializer_origin(self) -> str:
        return _origin(self.materializer_url)


def load_spu_settings(env_path: Path | None = None) -> SpuSettings:
    load_dotenv(env_path or PROJECT_ROOT / ".env")

    required = (
        "SPU_PORTAL_URL",
        "SPU_MATERIALIZER_URL",
        "SPU_LOGIN",
        "SPU_PASSWORD",
        "DATABASE_URL",
        "POSTGRES_SCHEMA",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Variaveis obrigatorias ausentes no .env: "
            + ", ".join(sorted(missing))
        )

    storage_state = Path(
        os.getenv(
            "SPU_STORAGE_STATE_PATH",
            str(DEFAULT_SPU_STORAGE_STATE_PATH),
        )
    ).expanduser()
    return SpuSettings(
        portal_url=os.environ["SPU_PORTAL_URL"].strip().rstrip("/"),
        materializer_url=os.environ["SPU_MATERIALIZER_URL"].strip().rstrip("/"),
        login=os.environ["SPU_LOGIN"].strip(),
        password=os.environ["SPU_PASSWORD"],
        database_url=normalize_postgres_url(os.environ["DATABASE_URL"]),
        postgres_schema=os.environ["POSTGRES_SCHEMA"].strip(),
        storage_state_path=storage_state,
        browser_headless=_parse_bool("SPU_BROWSER_HEADLESS", default=True),
        browser_executable_path=(
            os.getenv("SPU_BROWSER_EXECUTABLE_PATH", "").strip() or None
        ),
        browser_profile_dir=(
            Path(os.environ["SPU_BROWSER_PROFILE_DIR"]).expanduser()
            if os.getenv("SPU_BROWSER_PROFILE_DIR", "").strip()
            else None
        ),
        auto_renew_session=_parse_bool("SPU_AUTO_RENEW_SESSION", default=True),
        auth_timeout_seconds=_parse_positive_float(
            "SPU_AUTH_TIMEOUT_SECONDS", default=1800
        ),
        page_timeout_seconds=_parse_positive_float(
            "SPU_PAGE_TIMEOUT_SECONDS", default=90
        ),
        download_delay_seconds=_parse_nonnegative_float(
            "SPU_DOWNLOAD_DELAY_SECONDS", default=0.75
        ),
        process_batch_size=_parse_positive_int(
            "SPU_PROCESS_BATCH_SIZE", default=50
        ),
        pdf_batch_size=_parse_positive_int("SPU_PDF_BATCH_SIZE", default=20),
    )


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _parse_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "sim", "on"}:
        return True
    if value in {"0", "false", "no", "nao", "não", "off"}:
        return False
    raise RuntimeError(f"{name} deve ser true ou false.")


def _parse_positive_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser numerico.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} deve ser maior que zero.")
    return value


def _parse_nonnegative_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser numerico.") from exc
    if value < 0:
        raise RuntimeError(f"{name} deve ser maior ou igual a zero.")
    return value


def _parse_positive_int(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} deve ser um inteiro.") from exc
    if value <= 0:
        raise RuntimeError(f"{name} deve ser maior que zero.")
    return value
