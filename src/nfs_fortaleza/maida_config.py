from __future__ import annotations

import os
from dataclasses import dataclass


class MaidaConfigurationError(ValueError):
    """Raised when the Maida integration is not safely configured."""


@dataclass(frozen=True)
class MaidaSettings:
    login: str
    password: str
    database_url: str
    postgres_schema: str = "api_prontocardio"
    provider_id: str | None = None
    accounts_api_url: str = "https://accounts-api.issec.maida.health"
    onepass_url: str = "https://onepass.mv.com.br"
    users_api_url: str = "https://gestao-usuarios-api.issec.maida.health"
    provider_api_url: str = "https://credenciamento-api.issec.maida.health"
    billing_api_url: str = "https://faturamento-prestador-api.issec.maida.health"


def load_maida_settings() -> MaidaSettings:
    required = {
        "MAIDA_LOGIN": os.getenv("MAIDA_LOGIN", "").strip(),
        "MAIDA_PASSWORD": os.getenv("MAIDA_PASSWORD", ""),
        "DATABASE_URL": os.getenv("DATABASE_URL", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise MaidaConfigurationError(
            "Variaveis obrigatorias ausentes: " + ", ".join(sorted(missing))
        )

    return MaidaSettings(
        login=required["MAIDA_LOGIN"],
        password=required["MAIDA_PASSWORD"],
        database_url=required["DATABASE_URL"],
        postgres_schema=os.getenv("POSTGRES_SCHEMA", "api_prontocardio"),
        provider_id=os.getenv("MAIDA_PROVIDER_ID") or None,
        accounts_api_url=os.getenv(
            "MAIDA_ACCOUNTS_API_URL",
            "https://accounts-api.issec.maida.health",
        ).rstrip("/"),
        onepass_url=os.getenv(
            "MAIDA_ONEPASS_URL",
            "https://onepass.mv.com.br",
        ).rstrip("/"),
        users_api_url=os.getenv(
            "MAIDA_USERS_API_URL",
            "https://gestao-usuarios-api.issec.maida.health",
        ).rstrip("/"),
        provider_api_url=os.getenv(
            "MAIDA_PROVIDER_API_URL",
            "https://credenciamento-api.issec.maida.health",
        ).rstrip("/"),
        billing_api_url=os.getenv(
            "MAIDA_BILLING_API_URL",
            "https://faturamento-prestador-api.issec.maida.health",
        ).rstrip("/"),
    )
