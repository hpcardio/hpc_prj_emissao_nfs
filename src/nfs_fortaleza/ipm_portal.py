from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from dlt.sources.helpers.requests import Session

from nfs_fortaleza.ipm_config import (
    DEFAULT_IPM_DOWNLOADS_DIR,
    IpmSettings,
)


REFERENCE_PATTERN = re.compile(r"^(0[1-9]|1[0-2])/(\d{4})$")


class IpmPortalError(RuntimeError):
    """Raised when the IPM portal returns an unexpected response."""


@dataclass(frozen=True, order=True)
class IpmReference:
    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> IpmReference:
        match = REFERENCE_PATTERN.fullmatch(value.strip())
        if not match:
            raise ValueError(
                f"Referencia invalida: {value!r}. Use o formato MM/AAAA."
            )
        return cls(year=int(match.group(2)), month=int(match.group(1)))

    @property
    def label(self) -> str:
        return f"{self.month:02d}/{self.year:04d}"

    @property
    def api_value(self) -> str:
        return f"01/{self.label}"

    @property
    def file_name(self) -> str:
        return f"demonstrativo_conta_ipm_{self.year:04d}_{self.month:02d}.txt"


@dataclass(frozen=True)
class DownloadedIpmDemonstrative:
    reference: IpmReference
    path: Path


class IpmPortalClient:
    def __init__(
        self,
        settings: IpmSettings,
        *,
        downloads_dir: Path = DEFAULT_IPM_DOWNLOADS_DIR,
        timeout_seconds: float = 60,
    ) -> None:
        self.settings = settings
        self.downloads_dir = downloads_dir
        self.timeout_seconds = timeout_seconds
        self.session = Session()
        self.session.headers.update(
            {
                "User-Agent": "prj-web-nfs-ipm-dlt/1.0",
                "Accept": "application/json; charset=utf-8",
            }
        )
        self._authenticated = False

    def list_references(self) -> tuple[IpmReference, ...]:
        data = self._post_api(
            "Demonstrativo/ExtratoPagamentoCm",
            self._base_payload(),
        )
        if not isinstance(data, list):
            raise IpmPortalError(
                "A API do IPM retornou um formato invalido ao listar referencias."
            )

        references: set[IpmReference] = set()
        for item in data:
            if not isinstance(item, dict) or not item.get("mes_ano_ref"):
                raise IpmPortalError(
                    "A API do IPM retornou uma referencia sem mes_ano_ref."
                )
            try:
                references.add(IpmReference.parse(str(item["mes_ano_ref"])))
            except ValueError as exc:
                raise IpmPortalError(str(exc)) from exc
        return tuple(sorted(references))

    def download_demonstrative(
        self,
        reference: IpmReference,
    ) -> DownloadedIpmDemonstrative:
        payload = self._base_payload()
        payload["mesAnoRef"] = reference.api_value
        remote_path = self._post_api(
            "Demonstrativo/TxtDemonstrativoPagamentoCm",
            payload,
        )
        if not isinstance(remote_path, str) or not remote_path.strip():
            raise IpmPortalError(
                f"O portal nao gerou o arquivo da referencia {reference.label}."
            )

        normalized_path = remote_path.replace("\\", "/").lstrip("/")
        download_url = urljoin(self.settings.portal_origin + "/", normalized_path)
        if urlsplit(download_url).netloc != urlsplit(self.settings.portal_origin).netloc:
            raise IpmPortalError("O portal IPM retornou um endereco de download externo.")
        response = self.session.get(
            download_url,
            headers={
                "Accept": "text/plain, */*",
                "Content-Type": None,
                "Authorization": None,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        if b"FATURA#LOTE#" not in response.content:
            raise IpmPortalError(
                "O arquivo gerado pelo IPM nao contem o cabecalho esperado "
                f"para {reference.label}."
            )

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        destination = self.downloads_dir / reference.file_name
        temporary = destination.with_suffix(".txt.part")
        temporary.write_bytes(response.content)
        temporary.replace(destination)
        return DownloadedIpmDemonstrative(reference, destination)

    def _login(self) -> None:
        self.session.get(
            self.settings.portal_url,
            timeout=self.timeout_seconds,
        ).raise_for_status()
        response = self.session.post(
            self.settings.portal_url + "/Account/AutenticarCredenciado",
            data={
                "usuario": self.settings.login,
                "senha": self.settings.password,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        token = self.session.cookies.get("TOKEN")
        if not token:
            raise IpmPortalError(
                "Login no Portal Credenciado IPM recusado ou sem token de acesso."
            )
        self.session.headers.update(
            {
                "Authorization": token,
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        self._authenticated = True

    def _post_api(self, endpoint: str, payload: dict[str, str]):
        if not self._authenticated:
            self._login()
        response = self.session.post(
            f"{self.settings.api_url}/{endpoint.lstrip('/')}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise IpmPortalError(
                f"A API do IPM retornou uma resposta invalida em {endpoint}."
            ) from exc

    def _base_payload(self) -> dict[str, str]:
        return {
            "codPrestador": self.settings.provider_code,
            "codOperadora": self.settings.operator_code,
        }
