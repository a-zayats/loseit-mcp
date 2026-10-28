"""Re-extract Lose It's GWT build identifiers from its public web bundle.

Lose It's web app is a GWT app. Every RPC carries two identifiers tied to
the exact web build that is currently deployed:

* ``strong_name`` — the 32-character hex permutation hash. The bootstrap
  script (``web.nocache.js``) maps a browser/user-agent to one permutation,
  and the compiled file for that permutation is ``<strong_name>.cache.js``.
* ``policy_hash`` — the 32-character hex serialization-policy hash for the
  ``LoseItRemoteService``. It lives in that permutation's compiled file,
  in the ``LoseItRemoteService_Proxy`` constructor.

When Lose It ships a new build, both values change. The previously pinned
values are then rejected with ``IncompatibleRemoteServiceException`` — a
rejection that happens while the server is still deserializing the request,
before the service method runs, which is why a read (and, for this specific
error only, a write) is safe to retry once with fresh values.

This module only ever fetches *public* JavaScript: the bootstrap script
and one compiled permutation. It sends no credentials, no token, and no
account data, and it never touches the RPC endpoint itself.

Live format, verified Oct 28, 2026 against
``https://web.loseit.com/web/web.nocache.js`` (GWT 2.8.2, obfuscated):

* The bootstrap does not use ``answers['agent']`` mappings. It declares
  string variables (for example ``Mb='default'``, ``Yb='safari'``,
  ``uc='BFC4...'``) and registers permutations with
  ``k([Mb,Yb],uc)`` — that is, ``k([locale, agent], strong_name)``.
  Locale variants add a soft-permutation suffix (``uc+kc`` with
  ``kc=':1'``); the cache file name is the base strong name without
  that suffix, so suffixes are ignored when resolving a permutation.
* The compiled file registers the proxy type with
  ``nEv('com.loseit.core.client.service',
  'LoseItRemoteService_Proxy', <type_id>)`` (variable aliases resolved
  the same way), registers its constructor with
  ``$td(<type_id>, ..., <constructor>)``, and that constructor calls
  the RemoteServiceProxy super-constructor with the policy hash as a
  quoted argument. The policy hash is per-compilation, identical
  across all permutations of one build.
* The bundle is served from ``web.loseit.com/web/`` today, not from
  the older CloudFront host in the SDK defaults. Extraction therefore
  tries the configured module base first, then ``web.loseit.com/web/``.
* Permutations can be partially deployed: on Oct 28, 2026 the safari
  and gecko cache files returned 404 while the ie10 one loaded. The
  extractor therefore tries permutations in preference order and
  moves on when a cache file fails to load, instead of failing on
  the first missing file. A policy hash from any loaded permutation
  of the same build is the right hash for that build.

Any failure raises :class:`GwtExtractionError`. Callers must fall back to
the previously pinned values (and, for writes, must not blindly retry) —
a failed extraction is not a reason to invent identifiers.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_HEX32 = r"[0-9A-Fa-f]{32}"
_HEX_RE = re.compile(_HEX32)
_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"

# Older GWT bootstrap scripts map a user-agent key to a permutation hash
# with answers[...] spellings. Kept as a fallback for older builds.
_ANSWER_PATTERNS = (
    re.compile(r"answers\[['\"]([^'\"]+)['\"]\]\s*=\s*['\"](%s)['\"]" % _HEX32),
    re.compile(r"answers\.([A-Za-z0-9_]+)\s*=\s*['\"](%s)['\"]" % _HEX32),
    re.compile(r"['\"]([A-Za-z0-9_]+)['\"]\s*:\s*['\"](%s)['\"]" % _HEX32),
)

# k([locale, agent], strong_name) with optional +suffix for locale variants.
_K_PATTERN = re.compile(
    r"k\(\[(%s),(%s)\],(%s)(?:\+(%s))?\)" % (_IDENT, _IDENT, _IDENT, _IDENT)
)
# String variable assignments, e.g. Mb='default', uc='BFC4...'.
_VAR_PATTERN = re.compile(r"(%s)\s*=\s*'([^']*)'" % _IDENT)

# Preference order for choosing a permutation. Any current permutation
# should work as long as its policy hash is taken from the same file;
# Chrome first matches the permutation the operator last extracted by hand.
_PREFERRED_AGENTS = (
    "chrome",
    "safari",
    "gecko",
    "gecko1_8",
    "opera",
    "ie10",
    "ie9",
    "ie8",
)

# Fallback extraction base, in addition to the configured module base.
# Verified live Oct 28, 2026; the older CloudFront host is no longer
# serving the bundle.
_FALLBACK_BASE = "https://web.loseit.com/web/"


@dataclass(frozen=True)
class GwtBuild:
    strong_name: str
    policy_hash: str


class GwtExtractionError(RuntimeError):
    """The public bundle could not be parsed into a GWT build."""


def _resolve_string_vars(text: str) -> dict[str, str]:
    """Map variable names to their first single-quoted string value."""
    variables: dict[str, str] = {}
    for name, value in _VAR_PATTERN.findall(text):
        variables.setdefault(name, value)
    return variables


def _resolve_token(token: str, variables: dict[str, str]) -> str:
    """Resolve a literal or a variable name to its string value."""
    token = token.strip()
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    return variables.get(token, "")


def parse_permutations(nocache_text: str) -> dict[str, str]:
    """Return ``{user_agent: strong_name}`` from a bootstrap script.

    Tries the live GWT 2.8.2 ``k([locale, agent], strong_name)``
    registrations first, then the older ``answers[...]`` spellings.
    Returns an empty dict (rather than raising) when no mapping is
    found, so the caller can fall back to scanning for bare hex strings.
    """
    variables = _resolve_string_vars(nocache_text)
    found: dict[str, str] = {}
    for locale_token, agent_token, strong_token, _suffix in _K_PATTERN.findall(
        nocache_text
    ):
        agent = _resolve_token(agent_token, variables).lower()
        strong_name = _resolve_token(strong_token, variables).upper()
        if agent and re.fullmatch(_HEX32, strong_name) and agent not in found:
            # First registration per agent is its default locale; later
            # registrations for the same agent are locale variants of
            # the same base permutation.
            found[agent] = strong_name
    if found:
        return found
    for pattern in _ANSWER_PATTERNS:
        for agent, strong_name in pattern.findall(nocache_text):
            key = agent.lower()
            if key not in found and re.fullmatch(r"[a-z0-9_]+", key):
                found[key] = strong_name.upper()
    return found


def _ordered_permutations(
    permutations: dict[str, str],
    preferred_agents: tuple[str, ...],
) -> list[str]:
    """Permutation hashes in preference order, then file order."""
    ordered: list[str] = []
    for agent in preferred_agents:
        if agent in permutations:
            ordered.append(permutations[agent])
    for strong_name in permutations.values():
        if strong_name not in ordered:
            ordered.append(strong_name)
    return ordered


def choose_permutation(permutations: dict[str, str]) -> str:
    """Pick one permutation hash from a mapping, preferring known agents."""
    ordered = _ordered_permutations(permutations, _PREFERRED_AGENTS)
    if not ordered:
        raise GwtExtractionError("no permutation mapping found in nocache.js")
    return ordered[0]


def parse_policy_hash(cache_text: str) -> str:
    """Extract the LoseItRemoteService policy hash from a compiled file.

    Follows the obfuscated registration chain: ``nEv`` for
    ``LoseItRemoteService_Proxy`` gives a type id, ``$td`` for that id
    gives the proxy constructor, and that constructor's
    RemoteServiceProxy super-constructor call carries the policy hash
    as a quoted 32-hex argument.
    """
    variables = _resolve_string_vars(cache_text)
    # nEv(package, class_name, type_id), with literals or variable aliases.
    for package_token, class_token, type_id in re.findall(
        r"nEv\(\s*([^,]+),\s*([^,]+),\s*(\d+)\s*\)", cache_text
    ):
        package = _resolve_token(package_token, variables)
        class_name = _resolve_token(class_token, variables)
        if (
            package != "com.loseit.core.client.service"
            or class_name != "LoseItRemoteService_Proxy"
        ):
            continue
        # $td(type_id, ..., constructor)
        constructor_match = re.search(
            r"\$td\(\s*%s\s*,\s*\d+\s*,\s*\{[^}]*\}\s*,\s*(%s)\s*\)"
            % (type_id, _IDENT),
            cache_text,
        )
        if not constructor_match:
            continue
        constructor = constructor_match.group(1)
        function_match = re.search(
            r"function %s\(\)\{" % re.escape(constructor), cache_text
        )
        if not function_match:
            continue
        window = cache_text[
            function_match.end() : function_match.end() + 2000
        ]
        hash_match = re.search(r"'(%s)'" % _HEX32, window)
        if hash_match:
            return hash_match.group(1).upper()
    # Older, unobfuscated builds: a hex string shortly after the anchor.
    anchor = "LoseItRemoteService_Proxy"
    idx = cache_text.find(anchor)
    if idx != -1:
        window = cache_text[idx : idx + 4000]
        match = _HEX_RE.search(window)
        if match:
            return match.group(0).upper()
    # Last resort: a RemoteServiceProxy-style super-constructor call
    # carrying a quoted 32-hex argument. Any policy hash from this
    # build is better than failing, and a wrong one fails exactly as
    # the pinned values would.
    generic = re.search(
        r"\.call\(this,[^)]*?null,\s*'(" + _HEX32 + r")'", cache_text
    )
    if not generic:
        generic = re.search(
            r"null,\s*'(" + _HEX32 + r")'", cache_text
        )
    if generic:
        return generic.group(1).upper()
    raise GwtExtractionError(
        "no serialization-policy hash found for LoseItRemoteService_Proxy"
    )


def _fetch(url: str, *, timeout: float = 15.0) -> str:
    # httpx misparses bracketed IPv6 entries in no_proxy (for example
    # "[::1]" becomes a bogus ":1]" port). Strip those entries before
    # creating the client; the egress proxy itself is untouched.
    for key in ("no_proxy", "NO_PROXY"):
        value = os.environ.get(key)
        if value:
            os.environ[key] = ",".join(
                part
                for part in value.split(",")
                if not (part.startswith("[") and part.endswith("]"))
            )
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(
                url, headers={"User-Agent": "loseit-mcp gwt-extractor"}
            )
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise GwtExtractionError(f"could not fetch {url}: {exc}") from exc


def _candidate_bases(base_url: str) -> list[str]:
    bases = [base_url.rstrip("/") + "/"]
    if _FALLBACK_BASE not in bases:
        bases.append(_FALLBACK_BASE)
    return bases


def extract_build(
    base_url: str,
    *,
    fetch=_fetch,
    preferred_agents: tuple[str, ...] = _PREFERRED_AGENTS,
) -> GwtBuild:
    """Fetch the live bundle and return the current GWT build identifiers.

    ``base_url`` is the GWT module base, e.g.
    ``https://d3hsih69yn4d89.cloudfront.net/web/``. ``fetch`` is injectable
    so tests can supply recorded bundle text without network access.
    Tries the configured base first, then the live ``web.loseit.com``
    base, and tries permutations in preference order until one cache
    file loads and yields a policy hash — the bundle can be partially
    deployed, with some permutations returning 404.
    """
    nocache_text = None
    used_base = None
    last_error: Exception | None = None
    for base in _candidate_bases(base_url):
        try:
            nocache_text = fetch(base + "web.nocache.js")
            used_base = base
            break
        except GwtExtractionError as exc:
            last_error = exc
    if nocache_text is None or used_base is None:
        raise GwtExtractionError(
            f"could not fetch web.nocache.js from any base: {last_error}"
        )

    permutations = parse_permutations(nocache_text)
    if permutations:
        candidates = _ordered_permutations(permutations, preferred_agents)
    else:
        found_hex = [m.group(0).upper() for m in _HEX_RE.finditer(nocache_text)]
        seen: set[str] = set()
        candidates = [c for c in found_hex if not (c in seen or seen.add(c))]
    if not candidates:
        raise GwtExtractionError("no permutation hash found in web.nocache.js")

    errors: list[str] = []
    for strong_name in candidates:
        cache_url = used_base + strong_name + ".cache.js"
        try:
            cache_text = fetch(cache_url)
            policy_hash = parse_policy_hash(cache_text)
        except GwtExtractionError as exc:
            errors.append(f"{strong_name}: {exc}")
            continue
        logger.info(
            "extracted GWT build: strong_name=%s policy_hash=%s",
            strong_name,
            policy_hash,
        )
        return GwtBuild(strong_name=strong_name, policy_hash=policy_hash)
    raise GwtExtractionError(
        "no permutation cache file yielded a policy hash: " + "; ".join(errors)
    )
