"""GWT build auto-refresh: parsing, extraction, and retry behaviour.

The fixtures are recorded-style, not live bundle text: the point is that
the parser handles the mapping shapes GWT emits, that a strong name and a
policy hash are always taken as a pair from one permutation, and that a
stale-build error triggers exactly one refresh-and-retry — for reads, and
for writes only when the error is the pre-execution deserialization
rejection.
"""

from __future__ import annotations

from typing import Any

import pytest
from lose_it.core._http import LoseItError

from loseit_mcp import service as S
from loseit_mcp.gwt import (
    GwtBuild,
    GwtExtractionError,
    choose_permutation,
    extract_build,
    parse_permutations,
    parse_policy_hash,
)

NOCACHE = """
var answers = [];
answers['safari'] = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
answers['chrome'] = 'BFC488B2F9DCB4070D92CF9DD2B23810';
answers['gecko'] = 'CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC';
"""

CACHE = """
function LoseItRemoteService_Proxy() {
  var self = this;
  var policy = 'F5AA51DFF7600BE94631854AFC5DEFE9';
  return self;
}
"""

# Live GWT 2.8.2 shape, recorded Oct 28, 2026 from web.loseit.com:
# string variables plus k([locale, agent], strong_name) registrations.
NOCACHE_K = """
var Mb='default',Yb='safari',fc='gecko1_8',_b='ie10',jc='en',kc=':1';
var ic='120E404C664926D7C1F4334748C64561';
var tc='84F34F411B705956A3483E28DADF2903';
var uc='BFC488B2F9DCB4070D92CF9DD2B23810';
k([Mb,fc],ic);k([jc,fc],ic+kc);
k([Mb,_b],tc);k([jc,_b],tc+kc);
k([Mb,Yb],uc);k([jc,Yb],uc+kc);
"""

CACHE_OBFUSCATED = """
var r6v='com.loseit.core.client.service';
var s6v='LoseItRemoteService_Proxy';
function ngr(){var a;Pud.call(this,ai(),null,'F5AA51DFF7600BE94631854AFC5DEFE9',pdr);}
$td(1555,726,{},ngr);
var ZGc=nEv(r6v,s6v,1555);
"""


class TestParsing:
    def test_parse_permutations_from_answers_mapping(self) -> None:
        perms = parse_permutations(NOCACHE)
        assert perms["chrome"] == "BFC488B2F9DCB4070D92CF9DD2B23810"
        assert perms["safari"] == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_choose_prefers_chrome(self) -> None:
        assert choose_permutation(parse_permutations(NOCACHE)) == (
            "BFC488B2F9DCB4070D92CF9DD2B23810"
        )

    def test_parse_policy_hash_near_proxy(self) -> None:
        assert parse_policy_hash(CACHE) == "F5AA51DFF7600BE94631854AFC5DEFE9"

    def test_parse_policy_hash_fails_without_anchor_or_policy(self) -> None:
        with pytest.raises(GwtExtractionError):
            parse_policy_hash("function foo() { return 1; }")

    def test_parse_permutations_from_k_registrations(self) -> None:
        perms = parse_permutations(NOCACHE_K)
        assert perms["safari"] == "BFC488B2F9DCB4070D92CF9DD2B23810"
        assert perms["gecko1_8"] == "120E404C664926D7C1F4334748C64561"
        assert perms["ie10"] == "84F34F411B705956A3483E28DADF2903"

    def test_parse_policy_hash_from_obfuscated_chain(self) -> None:
        assert (
            parse_policy_hash(CACHE_OBFUSCATED)
            == "F5AA51DFF7600BE94631854AFC5DEFE9"
        )


class TestExtraction:
    def test_extract_pairs_strong_name_with_its_own_policy(self) -> None:
        def fake_fetch(url: str) -> str:
            if url.endswith("web.nocache.js"):
                return NOCACHE
            assert url.endswith(
                "BFC488B2F9DCB4070D92CF9DD2B23810.cache.js"
            ), url
            return CACHE

        build = extract_build("https://example.invalid/web/", fetch=fake_fetch)
        assert build == GwtBuild(
            strong_name="BFC488B2F9DCB4070D92CF9DD2B23810",
            policy_hash="F5AA51DFF7600BE94631854AFC5DEFE9",
        )

    def test_extract_falls_back_to_bare_hex_when_no_mapping(self) -> None:
        def fake_fetch(url: str) -> str:
            if url.endswith("web.nocache.js"):
                return "var x = 'DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD';"
            return CACHE

        build = extract_build("https://example.invalid/web/", fetch=fake_fetch)
        assert build.strong_name == "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD"

    def test_extract_fails_when_no_permutation(self) -> None:
        with pytest.raises(GwtExtractionError):
            extract_build(
                "https://example.invalid/web/",
                fetch=lambda url: "nothing here",
            )

    def test_extract_moves_to_next_permutation_on_cache_404(self) -> None:
        # Live state Oct 28, 2026: safari/gecko cache files 404, ie10 loads.
        def fake_fetch(url: str) -> str:
            if url.endswith("web.nocache.js"):
                return NOCACHE_K
            if url.endswith(
                "84F34F411B705956A3483E28DADF2903.cache.js"
            ):
                return CACHE_OBFUSCATED
            raise GwtExtractionError("404")

        build = extract_build("https://example.invalid/web/", fetch=fake_fetch)
        assert build == GwtBuild(
            strong_name="84F34F411B705956A3483E28DADF2903",
            policy_hash="F5AA51DFF7600BE94631854AFC5DEFE9",
        )

    def test_extract_falls_back_to_web_loseit_base(self) -> None:
        def fake_fetch(url: str) -> str:
            if url.startswith("https://example.invalid/"):
                raise GwtExtractionError("404")
            if url.endswith("web.nocache.js"):
                return NOCACHE
            return CACHE

        build = extract_build("https://example.invalid/web/", fetch=fake_fetch)
        assert build.strong_name == "BFC488B2F9DCB4070D92CF9DD2B23810"


class TestRefresh:
    def test_refresh_updates_settings_only_when_values_change(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = S.LoseItService(settings)
        old = (svc._settings.strong_name, svc._settings.policy_hash)
        monkeypatch.setattr(
            S,
            "extract_build",
            lambda base_url: GwtBuild(
                strong_name="EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE",
                policy_hash="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
            ),
        )
        assert svc._refresh_gwt_build() is True
        assert svc._settings.strong_name == "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE"
        assert svc._settings.policy_hash == "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"
        # Second refresh with the same values must not claim a change,
        # otherwise a retry loop could spin on a still-failing build.
        assert svc._refresh_gwt_build() is False
        assert old != (
            svc._settings.strong_name,
            svc._settings.policy_hash,
        )

    def test_refresh_failure_keeps_pinned_values(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = S.LoseItService(settings)
        before = svc._settings
        monkeypatch.setattr(
            S,
            "extract_build",
            lambda base_url: (_ for _ in ()).throw(
                S.GwtExtractionError("bundle gone")
            ),
        )
        assert svc._refresh_gwt_build() is False
        assert svc._settings is before

    def test_reading_retries_once_after_gwt_refresh(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = S.LoseItService(settings)
        monkeypatch.setattr(S.LoseItService, "_refresh_gwt_build", lambda self: True)
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise LoseItError("IncompatibleRemoteServiceException")
            return "ok"

        assert svc._reading(flaky, what="test") == "ok"
        assert attempts["n"] == 2

    def test_writing_retries_only_for_stale_build_error(
        self, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = S.LoseItService(settings)
        refreshed: list[int] = []
        monkeypatch.setattr(
            S.LoseItService,
            "_refresh_gwt_build",
            lambda self: refreshed.append(1) or True,
        )
        attempts = {"n": 0}

        def flaky() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise LoseItError("IncompatibleRemoteServiceException")
            return "ok"

        assert svc._retrying(flaky) == "ok"
        assert refreshed == [1]

        # A generic protocol error must NOT be retried for a write: it may
        # have been applied before the response failed to decode.
        with pytest.raises(LoseItError):
            svc._retrying(
                lambda: (_ for _ in ()).throw(LoseItError("Could not decode"))
            )
        assert refreshed == [1], "generic protocol errors must not refresh"
