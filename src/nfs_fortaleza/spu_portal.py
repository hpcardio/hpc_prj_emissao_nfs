from __future__ import annotations

import hashlib
import json
import logging
import fcntl
import re
import time
import unicodedata
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from nfs_fortaleza.spu_config import SpuSettings


LOGGER = logging.getLogger(__name__)


PROCESS_NUMBER_PATTERN = re.compile(r"\b(P?\d{5,})\s*[/_]\s*(\d{4})\b", re.I)
STATUS_PATTERN = re.compile(
    r"\b(ABERTURA\s+REPROVADA|FINALIZADO|TRAMITANDO|RASCUNHO|CANCELADO|"
    r"DESARQUIVADO|ARQUIVADO)\b",
    re.I,
)
DATE_PATTERN = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")


class SpuPortalError(RuntimeError):
    """Raised when the SPU portal returns an unexpected page or document."""


class SpuMaterializedTreeUnavailable(SpuPortalError):
    """Raised when a finalized process has no materialized process view."""


class SpuSessionExpiredError(SpuPortalError):
    """Raised when the persistent SPU profile requires human renewal."""


class SpuProfileInUseError(SpuPortalError):
    """Raised when another process is already using the Chromium profile."""


def _session_expired_error() -> SpuSessionExpiredError:
    return SpuSessionExpiredError(
        "A sessao persistida do SPU expirou e precisa de renovacao humana."
    )


@contextmanager
def spu_profile_lock(profile_dir: Path) -> Iterator[None]:
    """Prevent simultaneous Chromium processes from sharing one profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = profile_dir.parent / f".{profile_dir.name}.lock"
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SpuProfileInUseError(
                f"O perfil Chromium do SPU ja esta em uso: {profile_dir}. "
                "Feche a renovacao manual ou aguarde a DAG em execucao."
            ) from exc
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


@dataclass(frozen=True)
class SpuProcessSummary:
    numero_processo: str
    status_processo: str
    tipo_processo_assunto: str | None
    data_abertura: date | None
    motivo_finalizacao: str | None
    url_visualizacao: str | None

    @property
    def numero_ano(self) -> str:
        return self.numero_processo.replace("/", "_")

    @property
    def finalizado(self) -> bool:
        return _fold(self.status_processo) == "finalizado"


@dataclass(frozen=True)
class SpuDocument:
    numero_processo: str
    setor: str
    document_id: str
    nome: str
    path: Path


PROCESS_CARDS_SCRIPT = r"""
(root) => {
  const compact = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const numberRe = /\bP?\d{5,}\s*[\/_]\s*\d{4}\b/i;
  const reasonLabelRe =
    /\b(?:finalizado|desarquivado|arquivado|cancelado|abertura\s+reprovada)\b[\s\S]*?\bpelo\s+seguinte\s+motivo\b/i;
  let cards = Array.from(root.querySelectorAll('.card')).filter((card) => {
    const text = compact(card.innerText);
    return numberRe.test(text);
  });
  if (!cards.length) cards = [root];

  const results = [];
  for (const card of cards) {
    const header = card.querySelector('[id="step2-list-num"]');
    const headerText = compact(header?.innerText);
    const cardText = compact(card.innerText);
    const numberMatch = headerText.match(numberRe) || cardText.match(numberRe);
    if (!numberMatch) continue;

    const typeNode = card.querySelector('[id="step4-list-corpo-tipo-assunto"]');
    const dateNode = card.querySelector('[id="step7-list-corpo-data-criacao"]');
    const reasonLabel = Array.from(card.querySelectorAll('p')).find((node) =>
      reasonLabelRe.test(compact(node.innerText))
    );
    const reasonParagraphs = reasonLabel?.parentElement
      ? Array.from(reasonLabel.parentElement.querySelectorAll(':scope > p'))
      : [];
    const reasonIndex = reasonParagraphs.indexOf(reasonLabel);
    const reasonParagraph = reasonIndex >= 0
      ? reasonParagraphs.slice(reasonIndex + 1).find((node) => compact(node.innerText))
      : null;
    const reasonNode = reasonParagraph?.querySelector(
      'strong em span, strong em, em span, em, strong, span'
    ) || reasonParagraph;
    const links = Array.from(card.querySelectorAll('a'));
    const viewLink = links.find((link) =>
      /visualizar\s*(o\s*)?processo/i.test(compact(link.innerText)) ||
      /\/(?:materializar|visualizar_folder)(?:$|[?#])/i.test(
        link.getAttribute('href') || ''
      )
    );

    results.push({
      numero: numberMatch[0],
      cabecalho: headerText,
      texto: cardText,
      linhas: (card.innerText || '').split(/\r?\n/).map(compact).filter(Boolean),
      tipo: compact(typeNode?.innerText),
      data: compact(dateNode?.innerText),
      motivo: compact(reasonNode?.innerText),
      href: viewLink?.href || null,
    });
  }
  return results;
}
"""


class SpuPortalClient:
    def __init__(
        self,
        settings: SpuSettings,
        *,
        downloads_dir: Path,
    ) -> None:
        self.settings = settings
        self.downloads_dir = downloads_dir
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._profile_lock: AbstractContextManager[None] | None = None

    def __enter__(self) -> SpuPortalClient:
        try:
            self._start()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._context = None
                self._browser = None
                self._playwright = None
                self._page = None
                profile_lock = self._profile_lock
                self._profile_lock = None
                if profile_lock is not None:
                    profile_lock.__exit__(None, None, None)

    def list_processes(
        self,
        *,
        max_pages: int | None = None,
    ) -> tuple[SpuProcessSummary, ...]:
        found: dict[str, SpuProcessSummary] = {}
        for _, processes in self.iter_process_pages(max_pages=max_pages):
            for process in processes:
                found[process.numero_processo] = process
        return tuple(sorted(found.values(), key=lambda item: item.numero_processo))

    def iter_process_pages(
        self,
        *,
        max_pages: int | None = None,
    ) -> Iterator[tuple[int, tuple[SpuProcessSummary, ...]]]:
        """Yield parsed process cards after each SPU pagination request."""
        page = self._require_page()
        self._open_process_list(page)
        visited: set[str] = set()
        page_number = 0

        while True:
            page_number += 1
            self._wait_for_process_list(page)
            raw_cards = page.locator("#step-geral-listagem").evaluate(
                PROCESS_CARDS_SCRIPT
            )
            if not isinstance(raw_cards, list):
                raise SpuPortalError("A listagem do SPU retornou um formato invalido.")
            page_processes: dict[str, SpuProcessSummary] = {}
            for raw in raw_cards:
                process = parse_process_card(raw)
                page_processes[process.numero_processo] = process

            fingerprint = "|".join(sorted(page_processes))
            visit_key = f"{page.url}::{fingerprint}"
            if visit_key in visited:
                raise SpuPortalError("A paginacao do SPU entrou em um ciclo.")
            visited.add(visit_key)

            processes = tuple(
                sorted(
                    page_processes.values(),
                    key=lambda item: item.numero_processo,
                )
            )
            LOGGER.info(
                "SPU pagina %s carregada: %s processo(s), URL=%s",
                page_number,
                len(processes),
                page.url,
            )
            yield page_number, processes

            if max_pages is not None and page_number >= max_pages:
                break
            next_href = page.locator("#step-geral-listagem").evaluate(
                r"""
                (root) => {
                  const links = Array.from(document.querySelectorAll('.pagination a'));
                  const next = links.find((link) => {
                    const parent = link.closest('li');
                    if (parent?.classList.contains('disabled')) return false;
                    const text = (link.innerText || '').trim();
                    return link.rel === 'next' || parent?.classList.contains('next') ||
                      /^(proxima|próxima|next|›|»|>)$/i.test(text);
                  });
                  return next?.href || null;
                }
                """
            )
            if not next_href:
                break
            page.goto(
                urljoin(page.url, str(next_href)),
                wait_until="domcontentloaded",
                timeout=self.settings.page_timeout_seconds * 1000,
            )

    def _wait_for_process_list(self, page: Page) -> None:
        last_error: PlaywrightTimeoutError | None = None
        for attempt in range(3):
            try:
                page.wait_for_selector(
                    "#step-geral-listagem",
                    state="visible",
                    timeout=self.settings.page_timeout_seconds * 1000,
                )
                return
            except PlaywrightTimeoutError as exc:
                last_error = exc
                if self._is_login_page(page):
                    raise _session_expired_error() from exc
                if attempt < 2:
                    page.reload(
                        wait_until="domcontentloaded",
                        timeout=self.settings.page_timeout_seconds * 1000,
                    )
        raise SpuPortalError(
            f"A listagem do SPU nao carregou apos tres tentativas em {page.url}."
        ) from last_error

    def download_process_documents(
        self,
        process: SpuProcessSummary,
    ) -> tuple[SpuDocument, ...]:
        if not process.finalizado:
            return ()
        page = self._require_page()
        tree = self._open_folder_tree(page, process)
        process_data = tree.get("processo")
        if not isinstance(process_data, dict):
            raise SpuPortalError(
                f"Arvore de documentos invalida para {process.numero_processo}."
            )
        numero_ano = str(process_data.get("numeroAno") or process.numero_ano)
        itens = process_data.get("itens")
        setores = itens.get("setores") if isinstance(itens, dict) else None
        if not isinstance(setores, list):
            raise SpuPortalError(
                f"Setores de documentos ausentes em {process.numero_processo}."
            )

        targets = {"ipm/saudecogestao", "ipm/nuexo"}
        selected: list[tuple[str, dict[str, Any]]] = []
        available: list[str] = []
        found_target_sectors: set[str] = set()
        empty_target_sectors: list[str] = []
        for item in setores:
            if not isinstance(item, dict):
                continue
            sector = str(item.get("setor") or "").strip()
            available.append(sector)
            normalized_sector = canonical_spu_sector(sector)
            if normalized_sector not in targets:
                continue
            found_target_sectors.add(normalized_sector)
            documents = item.get("documentos")
            if not isinstance(documents, list) or not documents:
                empty_target_sectors.append(sector)
                continue
            for document in documents:
                if not isinstance(document, dict):
                    continue
                document_name = str(document.get("nome") or "").strip()
                if _is_target_document(normalized_sector, document_name):
                    selected.append((sector, document))
                else:
                    LOGGER.debug(
                        "SPU PDFs: ignorando documento fora do escopo %r em %s/%s.",
                        document_name,
                        process.numero_processo,
                        sector,
                    )

        if not found_target_sectors:
            LOGGER.info(
                "SPU PDFs: processo %s nao possui setores alvo. Setores: %s",
                process.numero_processo,
                ", ".join(available),
            )
            return ()
        missing = sorted(targets - found_target_sectors)
        if missing or empty_target_sectors:
            details: list[str] = []
            if missing:
                details.append("ausentes: " + ", ".join(missing))
            if empty_target_sectors:
                details.append("sem documentos: " + ", ".join(empty_target_sectors))
            LOGGER.warning(
                "SPU PDFs: setores alvo parciais em %s (%s). "
                "Setores encontrados: %s.",
                process.numero_processo,
                "; ".join(details),
                ", ".join(available),
            )

        downloads: list[SpuDocument] = []
        for sector, document in selected:
            document_id = str(document.get("id") or "").strip()
            name = str(document.get("nome") or f"documento_{document_id}.pdf").strip()
            if not document_id:
                raise SpuPortalError(
                    f"Documento sem ID em {process.numero_processo}/{sector}."
                )
            path = self._download_pdf(
                process,
                numero_ano=numero_ano,
                sector=sector,
                document_id=document_id,
                name=name,
            )
            downloads.append(
                SpuDocument(
                    numero_processo=process.numero_processo,
                    setor=sector,
                    document_id=document_id,
                    nome=name,
                    path=path,
                )
            )
        return tuple(downloads)

    def _start(self) -> None:
        profile_dir = self.settings.browser_profile_dir
        if profile_dir is not None:
            self._profile_lock = spu_profile_lock(profile_dir)
            self._profile_lock.__enter__()

        self._playwright = sync_playwright().start()
        launch_options: dict[str, Any] = {
            "headless": self.settings.browser_headless,
        }
        if self.settings.browser_executable_path:
            launch_options["executable_path"] = self.settings.browser_executable_path
        if profile_dir is not None:
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                accept_downloads=True,
                locale="pt-BR",
                timezone_id="America/Fortaleza",
                **launch_options,
            )
            self._context.set_default_timeout(
                self.settings.page_timeout_seconds * 1000
            )
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
            return

        self._browser = self._playwright.chromium.launch(**launch_options)
        state_path = self.settings.storage_state_path
        context_options: dict[str, Any] = {
            "accept_downloads": True,
            "locale": "pt-BR",
            "timezone_id": "America/Fortaleza",
        }
        if state_path.is_file():
            context_options["storage_state"] = str(state_path)
        try:
            self._context = self._browser.new_context(**context_options)
        except Exception as exc:
            if "storage_state" not in context_options:
                raise
            raise SpuPortalError(
                f"Sessao persistida do SPU invalida em {state_path}."
            ) from exc
        self._context.set_default_timeout(self.settings.page_timeout_seconds * 1000)
        self._page = self._context.new_page()

    def _open_process_list(self, page: Page) -> None:
        process_url = self.settings.portal_origin + "/processos/usuario"
        page.goto(
            process_url,
            wait_until="domcontentloaded",
            timeout=self.settings.page_timeout_seconds * 1000,
        )
        if self._is_login_page(page):
            raise _session_expired_error()
        if "/browser_support" in page.url:
            raise SpuPortalError("O SPU recusou a versao do Chromium instalada.")

        virtual_tab = page.locator("#step-processos-virtuais").first
        if virtual_tab.count() and not page.locator("#step-geral-listagem").count():
            virtual_tab.click()
            page.wait_for_load_state("domcontentloaded")

    def _open_folder_tree(
        self,
        page: Page,
        process: SpuProcessSummary,
    ) -> dict[str, Any]:
        if not process.url_visualizacao:
            raise SpuMaterializedTreeUnavailable(
                f"Processo {process.numero_processo} finalizado sem o botao "
                "VISUALIZAR PROCESSO."
            )
        url = process.url_visualizacao
        self._validate_navigation_url(url)
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=self.settings.page_timeout_seconds * 1000,
        )
        if self._is_login_page(page):
            raise _session_expired_error()

        folder_link = page.locator("a[href*='/visualizar_folder']").first
        if folder_link.count() and not page.locator(
            "[data-react-class*='FolderProcessoSo']"
        ).count():
            href = folder_link.get_attribute("href")
            if href:
                page.goto(
                    urljoin(page.url, href),
                    wait_until="domcontentloaded",
                    timeout=self.settings.page_timeout_seconds * 1000,
                )
                if self._is_login_page(page):
                    raise _session_expired_error()

        component = page.locator("[data-react-class*='FolderProcessoSo']").first
        try:
            component.wait_for(
                state="attached",
                timeout=min(self.settings.page_timeout_seconds, 5) * 1000,
            )
        except PlaywrightTimeoutError as exc:
            raise SpuMaterializedTreeUnavailable(
                f"Arvore materializada nao encontrada para {process.numero_processo}."
            ) from exc
        raw = component.get_attribute("data-react-props")
        if not raw:
            raise SpuPortalError(
                f"Dados da arvore ausentes para {process.numero_processo}."
            )
        try:
            props = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SpuPortalError(
                f"Dados da arvore invalidos para {process.numero_processo}."
            ) from exc
        tree = props.get("dataTree") if isinstance(props, dict) else None
        if not isinstance(tree, dict):
            raise SpuPortalError(
                f"dataTree ausente para {process.numero_processo}."
            )
        return tree

    def _download_pdf(
        self,
        process: SpuProcessSummary,
        *,
        numero_ano: str,
        sector: str,
        document_id: str,
        name: str,
    ) -> Path:
        context = self._require_context()
        directory = (
            self.downloads_dir
            / _safe_name(process.numero_ano)
            / _safe_name(sector)
        )
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / (
            f"{_safe_name(Path(name).stem)}_{_safe_name(document_id)}.pdf"
        )
        if destination.is_file():
            with destination.open("rb") as existing:
                if existing.read(4) == b"%PDF":
                    LOGGER.debug("SPU PDFs: reutilizando %s.", destination)
                    return destination
        url = (
            f"{self.settings.materializer_origin}/processos/"
            f"{quote(numero_ano, safe='_')}/documento_so/"
            f"{quote(document_id, safe='')}/pdf?download_file=true"
        )
        response = None
        for attempt in range(1, 5):
            if self.settings.download_delay_seconds:
                time.sleep(self.settings.download_delay_seconds)
            response = context.request.get(
                url,
                headers={"Referer": self._require_page().url},
                timeout=self.settings.page_timeout_seconds * 1000,
            )
            if response.ok:
                break
            retryable = response.status in {429, 500, 502, 503, 504}
            if not retryable or attempt == 4:
                raise SpuPortalError(
                    f"Falha HTTP {response.status} ao baixar {name!r} "
                    f"de {process.numero_processo}."
                )
            retry_after = response.headers.get("retry-after", "").strip()
            try:
                wait_seconds = float(retry_after)
            except ValueError:
                wait_seconds = float(2**attempt)
            wait_seconds = min(max(wait_seconds, 1.0), 30.0)
            LOGGER.warning(
                "SPU PDFs: HTTP %s em %s/%r; nova tentativa %s/4 em %.1fs.",
                response.status,
                process.numero_processo,
                name,
                attempt + 1,
                wait_seconds,
            )
            time.sleep(wait_seconds)
        if response is None:
            raise SpuPortalError(
                f"Download nao iniciado para {name!r} de {process.numero_processo}."
            )
        content = response.body()
        if not content.startswith(b"%PDF"):
            raise SpuPortalError(
                f"Documento {name!r} de {process.numero_processo} nao e PDF."
            )

        temporary = destination.with_suffix(".pdf.part")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination

    def _validate_navigation_url(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        allowed = {
            urlsplit(self.settings.portal_origin).netloc.lower(),
            urlsplit(self.settings.materializer_origin).netloc.lower(),
        }
        if host not in allowed:
            raise SpuPortalError(f"URL externa recusada pelo extrator SPU: {host}.")

    @staticmethod
    def _is_login_page(page: Page) -> bool:
        return "/auth/login" in page.url or bool(
            page.locator("input[name='user[login]']").count()
        )

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("SpuPortalClient deve ser usado como context manager.")
        return self._context

    def _require_page(self) -> Page:
        if self._page is None:
            raise RuntimeError("SpuPortalClient deve ser usado como context manager.")
        return self._page


def parse_process_card(raw: object) -> SpuProcessSummary:
    if not isinstance(raw, dict):
        raise SpuPortalError("Card de processo invalido na listagem do SPU.")
    text = _compact(str(raw.get("texto") or ""))
    header = _compact(str(raw.get("cabecalho") or ""))
    number_match = PROCESS_NUMBER_PATTERN.search(str(raw.get("numero") or header))
    if not number_match:
        raise SpuPortalError("Numero do processo ausente em um card do SPU.")
    number = f"{number_match.group(1).upper()}/{number_match.group(2)}"
    status_match = STATUS_PATTERN.search(header) or STATUS_PATTERN.search(text)
    status = (
        status_match.group(1).upper()
        if status_match
        else _status_from_header(header) or "DESCONHECIDO"
    )
    if status == "DESCONHECIDO":
        LOGGER.warning(
            "Status nao reconhecido no processo %s; usando DESCONHECIDO. Texto: %s",
            number,
            text[:500],
        )

    raw_type = _compact(str(raw.get("tipo") or ""))
    lines = [
        _compact(str(line))
        for line in raw.get("linhas", [])
        if _compact(str(line))
    ]
    process_type = (
        _clean_labeled_value(
            raw_type,
            ("TIPO DE PROCESSO/ASSUNTO", "TIPO/ASSUNTO", "ASSUNTO"),
            stop_labels=("DATA DE ABERTURA", "MOTIVO DA FINALIZACAO", "MOTIVO"),
        )
        if raw_type
        else _type_from_card_lines(lines, status)
    )
    raw_date = _compact(str(raw.get("data") or ""))
    date_match = DATE_PATTERN.search(raw_date) or DATE_PATTERN.search(text)
    opened_at = (
        datetime.strptime(date_match.group(1), "%d/%m/%Y").date()
        if date_match
        else None
    )
    reason = _clean_reason(str(raw.get("motivo") or ""))
    href = str(raw.get("href") or "").strip() or None
    return SpuProcessSummary(
        numero_processo=number,
        status_processo=status,
        tipo_processo_assunto=process_type,
        data_abertura=opened_at,
        motivo_finalizacao=reason,
        url_visualizacao=href,
    )


def _status_from_header(header: str) -> str | None:
    number_match = PROCESS_NUMBER_PATTERN.search(header)
    if not number_match:
        return None
    candidate = _compact(header[number_match.end() :]).strip(" :-–—")
    if not candidate or len(candidate) > 80:
        return None
    return candidate.upper()


def _clean_labeled_value(
    text: str,
    labels: tuple[str, ...],
    *,
    stop_labels: tuple[str, ...],
) -> str | None:
    folded = _fold(text)
    start = -1
    label_length = 0
    for label in labels:
        index = folded.find(_fold(label))
        if index >= 0 and (start < 0 or index < start):
            start = index
            label_length = len(label)
    if start < 0:
        value = _compact(text)
        return value or None
    value_start = start + label_length
    value_end = len(text)
    folded_tail = folded[value_start:]
    for label in stop_labels:
        index = folded_tail.find(_fold(label))
        if index >= 0:
            value_end = min(value_end, value_start + index)
    value = text[value_start:value_end].lstrip(" :-–—")
    value = re.sub(r"\b(FINALIZADO|TRAMITANDO)\b.*$", "", value, flags=re.I)
    return _compact(value) or None


def _clean_reason(value: str) -> str | None:
    reason = _compact(value)
    reason = re.split(
        r"\s+VISUALIZAR\s+(?:MOVIMENTA\S*|PROCESSO)\b",
        reason,
        maxsplit=1,
        flags=re.I,
    )[0]
    reason = re.sub(r"\s*\(ver\s+(?:menos|mais)\).*$", "", reason, flags=re.I)
    reason = reason.strip(' "\'“”‘’')
    return reason or None


def _type_from_card_lines(lines: list[str], status: str) -> str | None:
    status_index = next(
        (index for index, line in enumerate(lines) if _fold(line) == _fold(status)),
        -1,
    )
    if status_index < 0:
        return None
    candidates: list[str] = []
    for line in lines[status_index + 1 :]:
        folded = _fold(line)
        if DATE_PATTERN.search(line) or "finalizado pelo" in folded:
            break
        if folded in {"numero", "atencao"}:
            continue
        candidates.append(line)
        if len(candidates) >= 2:
            break
    return _compact(" ".join(candidates)) or None


def _compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char)).lower()


def _fold_sector(value: str) -> str:
    return re.sub(r"\s+", "", _fold(value)).strip("/")


def canonical_spu_sector(value: str) -> str:
    normalized = _fold_sector(value)
    if normalized in {
        "ipm/saudecogestao",
        "ipm/co-gestora",
        "ipm/cogestora",
    }:
        return "ipm/saudecogestao"
    return normalized


def _is_target_document(sector: str, name: str) -> bool:
    folded = _fold(name)
    if not folded:
        return True
    if sector == "ipm/saudecogestao":
        if re.fullmatch(
            r"relatorio_\d+(?:_.*)?",
            Path(folded).stem,
        ):
            return False
        return "despacho" not in folded
    if sector == "ipm/nuexo":
        excluded = (
            "certidao",
            "certidoes",
            "consulta",
            "regularidade",
            "cnd",
            "fgts",
            "crf",
        )
        return not any(term in folded for term in excluded)
    return False


def _safe_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_value).strip("._")
    return safe[:120] or "documento"
