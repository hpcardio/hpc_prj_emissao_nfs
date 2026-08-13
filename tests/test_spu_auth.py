from __future__ import annotations

from nfs_fortaleza.spu_auth import (
    _prefill_login_form,
    _wait_for_human_login,
)


class FakeLocator:
    def __init__(self, *, count: int = 1) -> None:
        self._count = count
        self.value = ""
        self.checked = False

    @property
    def first(self) -> FakeLocator:
        return self

    def wait_for(self, **_kwargs: object) -> None:
        return None

    def fill(self, value: str) -> None:
        self.value = value

    def count(self) -> int:
        return self._count

    def is_checked(self) -> bool:
        return self.checked

    def check(self, **_kwargs: object) -> None:
        self.checked = True


class FakeLoginPage:
    def __init__(self) -> None:
        self.url = "https://spuvirtual.sepog.fortaleza.ce.gov.br/auth/login"
        self.login = FakeLocator()
        self.password = FakeLocator()
        self.remember = FakeLocator()

    def locator(self, selector: str) -> FakeLocator:
        if "password" in selector:
            return self.password
        if "remember" in selector or "checkbox" in selector:
            return self.remember
        if "/auth/login" in self.url:
            return self.login
        return FakeLocator(count=0)

    def is_closed(self) -> bool:
        return False

    def wait_for_timeout(self, _milliseconds: float) -> None:
        self.url = (
            "https://spuvirtual.sepog.fortaleza.ce.gov.br/processos/usuario"
        )


def test_prefill_login_form_fills_credentials_and_remember_checkbox() -> None:
    page = FakeLoginPage()

    _prefill_login_form(page, "usuario-teste", "senha-teste")  # type: ignore[arg-type]

    assert page.login.value == "usuario-teste"
    assert page.password.value == "senha-teste"
    assert page.remember.checked is True


def test_wait_for_human_login_returns_after_leaving_login_page() -> None:
    page = FakeLoginPage()

    _wait_for_human_login(page, timeout_seconds=2)  # type: ignore[arg-type]

    assert page.url.endswith("/processos/usuario")
