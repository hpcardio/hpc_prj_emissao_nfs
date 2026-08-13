from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from nfs_fortaleza.config import PROJECT_ROOT, normalize_postgres_url


DEFAULT_IPM_DOWNLOADS_DIR = PROJECT_ROOT / "downloads" / "ipm"


@dataclass(frozen=True)
class IpmSettings:
    portal_url: str
    login: str
    password: str
    database_url: str
    postgres_schema: str
    provider_code: str
    operator_code: str

    @property
    def portal_origin(self) -> str:
        parsed = urlsplit(self.portal_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @property
    def api_url(self) -> str:
        return self.portal_origin + "/api/APICredenciado/api"


def load_ipm_settings(env_path: Path | None = None) -> IpmSettings:
    load_dotenv(env_path or PROJECT_ROOT / ".env")

    required = (
        "IPM_PORTAL_URL",
        "IPM_LOGIN",
        "IPM_PASSWORD",
        "DATABASE_URL",
        "POSTGRES_SCHEMA",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Variaveis obrigatorias ausentes no .env: "
            + ", ".join(sorted(missing))
        )

    login = os.environ["IPM_LOGIN"].strip()
    return IpmSettings(
        portal_url=os.environ["IPM_PORTAL_URL"].strip().rstrip("/"),
        login=login,
        password=os.environ["IPM_PASSWORD"],
        database_url=normalize_postgres_url(os.environ["DATABASE_URL"]),
        postgres_schema=os.environ["POSTGRES_SCHEMA"].strip(),
        provider_code=os.getenv("IPM_PROVIDER_CODE", login).strip(),
        operator_code=os.getenv("IPM_OPERATOR_CODE", "1").strip(),
    )
