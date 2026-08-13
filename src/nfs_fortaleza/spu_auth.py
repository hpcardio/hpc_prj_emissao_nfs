from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from nfs_fortaleza.spu_config import (
    DEFAULT_SPU_BROWSER_PROFILE_DIR,
    DEFAULT_SPU_DOWNLOADS_DIR,
    SpuSettings,
    load_spu_settings,
)
from nfs_fortaleza.spu_portal import SpuPortalClient, spu_profile_lock


LOGGER = logging.getLogger(__name__)


class SpuInteractiveAuthError(RuntimeError):
    """Raised when the visible SPU session renewal cannot be completed."""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Abre o SPU em um perfil Chromium isolado, preenche as "
            "credenciais e aguarda o usuario concluir a autenticacao."
        )
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=DEFAULT_SPU_BROWSER_PROFILE_DIR,
        help="Diretorio persistente do perfil isolado do SPU.",
    )
    parser.add_argument(
        "--executable-path",
        help="Caminho para um Chrome/Chromium instalado.",
    )
    args = parser.parse_args()

    settings = load_spu_settings()
    profile_dir = args.browser_profile.expanduser().resolve()
    executable = (
        args.executable_path
        or settings.browser_executable_path
        or _find_browser_executable()
    )

    renew_spu_session(
        settings,
        profile_dir=profile_dir,
        executable_path=executable,
    )
    validation_settings = replace(
        settings,
        browser_headless=True,
        browser_executable_path=executable,
        browser_profile_dir=profile_dir,
    )
    with SpuPortalClient(
        validation_settings,
        downloads_dir=DEFAULT_SPU_DOWNLOADS_DIR,
    ) as client:
        processes = client.list_processes(max_pages=1)
    print(
        f"Perfil autenticado gravado em {profile_dir}. "
        f"Primeira pagina validada com {len(processes)} processos."
    )


def renew_spu_session(
    settings: SpuSettings,
    *,
    profile_dir: Path | None = None,
    executable_path: str | None = None,
    timeout_seconds: float | None = None,
) -> None:
    """Open a visible browser, prefill credentials and wait for human login."""
    _ensure_visible_browser_available()
    profile_dir = profile_dir or settings.browser_profile_dir
    if profile_dir is None:
        raise SpuInteractiveAuthError(
            "SPU_BROWSER_PROFILE_DIR deve apontar para um perfil persistente."
        )
    profile_dir = profile_dir.expanduser().resolve()
    timeout_seconds = timeout_seconds or settings.auth_timeout_seconds
    profile_dir.mkdir(parents=True, exist_ok=True)

    process_url = settings.portal_origin + "/processos/usuario"
    launch_options: dict[str, object] = {
        "headless": False,
        "accept_downloads": True,
        "locale": "pt-BR",
        "timezone_id": "America/Fortaleza",
        "no_viewport": True,
        "args": ["--start-maximized"],
        "ignore_default_args": ["--enable-automation"],
    }
    executable = (
        executable_path
        or settings.browser_executable_path
        or _find_browser_executable()
    )
    if executable:
        launch_options["executable_path"] = executable

    with spu_profile_lock(profile_dir):
        playwright = sync_playwright().start()
        context: BrowserContext | None = None
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **launch_options,
            )
            context.set_default_timeout(settings.page_timeout_seconds * 1000)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                process_url,
                wait_until="domcontentloaded",
                timeout=settings.page_timeout_seconds * 1000,
            )
            if not _is_spu_login_page(page):
                LOGGER.info("A sessao persistida do SPU ainda esta valida.")
                return

            _prefill_login_form(page, settings.login, settings.password)
            page.bring_to_front()
            LOGGER.warning(
                "Sessao SPU expirada. A janela de login foi aberta com as "
                "credenciais preenchidas. Conclua o reCAPTCHA e clique em "
                "Entrar; a janela sera fechada automaticamente."
            )
            _wait_for_human_login(
                page,
                timeout_seconds=timeout_seconds,
            )
            LOGGER.info("Sessao SPU renovada pelo usuario.")
        except SpuInteractiveAuthError:
            raise
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            raise SpuInteractiveAuthError(
                "Nao foi possivel concluir a renovacao visivel da sessao SPU."
            ) from exc
        finally:
            if context is not None:
                with suppress(PlaywrightError):
                    context.close()
            playwright.stop()


def _prefill_login_form(page: Page, login: str, password: str) -> None:
    login_input = page.locator(
        "input[name='user[login]'], input[name='login'], input[type='email']"
    ).first
    password_input = page.locator(
        "input[name='user[password]'], input[name='password'], "
        "input[type='password']"
    ).first
    try:
        login_input.wait_for(state="visible")
        password_input.wait_for(state="visible")
        login_input.fill(login)
        password_input.fill(password)

        remember = page.locator(
            "input[type='checkbox'][name='user[remember_me]'], "
            "input[type='checkbox'][name*='remember'], "
            "input[type='checkbox'][id*='remember'], input[type='checkbox']"
        ).first
        if remember.count() and not remember.is_checked():
            remember.check(force=True)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise SpuInteractiveAuthError(
            "O formulario de login do SPU mudou e nao pode ser preenchido."
        ) from exc


def _wait_for_human_login(page: Page, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if page.is_closed():
            raise SpuInteractiveAuthError(
                "A janela do SPU foi fechada antes da autenticacao."
            )
        if not _is_spu_login_page(page):
            return
        try:
            page.wait_for_timeout(500)
        except PlaywrightError as exc:
            raise SpuInteractiveAuthError(
                "A janela do SPU foi fechada antes da autenticacao."
            ) from exc
    raise SpuInteractiveAuthError(
        "Tempo esgotado aguardando a autenticacao humana no SPU."
    )


def _is_spu_login_page(page: Page) -> bool:
    return "/auth/login" in urlsplit(page.url).path or bool(
        page.locator("input[name='user[login]']").count()
    )


def _ensure_visible_browser_available() -> None:
    if sys.platform.startswith("linux") and not (
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    ):
        raise SpuInteractiveAuthError(
            "Nenhuma sessao grafica foi disponibilizada ao scheduler. "
            "Configure DISPLAY e o socket grafico no Docker."
        )


def _find_browser_executable() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


if __name__ == "__main__":
    main()
