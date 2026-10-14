"""Tier-5 extraction: trimming, credential resolution, and response handling.

Two things are pinned here.

The first is the resolution order. The key used to be read from os.environ while
the Settings page wrote it to the database, so a key entered in the UI was
stored, shown as active on the dashboard, and then silently ignored -- pages
that needed tier 5 just came back with no price. That is a failure with no error
message anywhere, so it gets a test.

The second is that a bad response never becomes a price. A truncated or refused
completion must yield "no price", not a half-parsed number: a wrong price here
opens a MAP violation against a dealer.

No network, no credentials. The OpenAI client is stubbed.
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from worker import extract

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


# --- stub settings_store so nothing touches a database -----------------------
_STORE = {}


async def _fake_get(key, default=None):
    val = _STORE.get(key)
    return val if val not in (None, "") else default


async def _fake_get_int(key, default):
    try:
        return int(await _fake_get(key)) if _STORE.get(key) else default
    except (TypeError, ValueError):
        return default


extract.settings_store = types.SimpleNamespace(get=_fake_get, get_int=_fake_get_int)


print("1. Credentials resolve through settings_store, not the environment")
_STORE.clear()
check("no key -> disabled", asyncio.run(extract.llm_enabled()), False)
_STORE["openai_api_key"] = "sk-test-key"
check("key in store -> enabled", asyncio.run(extract.llm_enabled()), True)
key, model, chars = asyncio.run(extract._llm_config())
check("key read from store", key, "sk-test-key")
check("model falls back to default", model, extract.DEFAULT_EXTRACTION_MODEL)
_STORE["extraction_model"] = "gpt-4o"
check("model override honoured", asyncio.run(extract._llm_config())[1], "gpt-4o")
_STORE["llm_max_input_chars"] = "1234"
check("max chars override honoured", asyncio.run(extract._llm_config())[2], 1234)


print("\n2. Trimming keeps the price region and bounds the payload")
page = "<html><body>" + "<p>filler line</p>" * 400 + \
       "<p>Regular price $97.20</p><p>Sale price $86.98</p>" + \
       "<p>tail</p>" * 200 + "</body></html>"
trimmed = extract.trim_html_for_llm(page, 24_000)
check("keeps the sale price", "$86.98" in trimmed, True)
check("keeps the list price", "$97.20" in trimmed, True)
check("respects the cap", len(extract.trim_html_for_llm(page, 500)) <= 500, True)
check("script/style stripped",
      "filler" in extract.trim_html_for_llm("<html><body><script>var x=1</script>"
                                            "<p>filler $1.00</p></body></html>", 5000),
      True)


# --- stub the OpenAI client --------------------------------------------------
class _Msg:
    def __init__(self, content, refusal=None):
        self.content, self.refusal = content, refusal


class _Choice:
    def __init__(self, content, finish_reason="stop", refusal=None):
        self.message, self.finish_reason = _Msg(content, refusal), finish_reason


def _install_openai(choice):
    class _Completions:
        async def create(self, **kw):
            _install_openai.last_kwargs = kw
            return types.SimpleNamespace(choices=[choice] if choice else [])

    class _AsyncOpenAI:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=_Completions())

        async def close(self):
            pass

    sys.modules["openai"] = types.SimpleNamespace(AsyncOpenAI=_AsyncOpenAI)


HTML = "<html><body><p>Sale price $86.98</p></body></html>"
GOOD = ('{"price": 86.98, "currency": "USD", "in_stock": true, '
        '"title": "Onyx", "sku": "AB-0004-02", "upc": null}')

print("\n3. A well-formed reply becomes a price")
_STORE.clear(); _STORE["openai_api_key"] = "sk-test-key"
_install_openai(_Choice(GOOD))
res = asyncio.run(extract.from_llm(HTML, "https://example.com/p"))
check("price parsed", float(res.price), 86.98)
check("tier reported", res.tier, "llm")
check("stock parsed", res.in_stock, True)
check("model recorded", res.raw["model"], extract.DEFAULT_EXTRACTION_MODEL)
check("strict schema requested",
      _install_openai.last_kwargs["response_format"]["json_schema"]["strict"], True)

print("\n4. A bad reply never becomes a price")
for label, choice in [
    ("truncated (finish_reason=length)", _Choice(GOOD, finish_reason="length")),
    ("refused", _Choice(None, refusal="I can't help with that")),
    ("prose instead of JSON", _Choice("The price is $86.98")),
    ("empty content", _Choice("")),
    ("no choices at all", None),
]:
    _install_openai(choice)
    r = asyncio.run(extract.from_llm(HTML, "https://example.com/p"))
    check(label, (r.price, r.tier), (None, "none"))

print("\n5. Disabled means no call is attempted")
_STORE.clear()
_install_openai(_Choice(GOOD))
_install_openai.last_kwargs = None
r = asyncio.run(extract.from_llm(HTML, "https://example.com/p"))
check("no key -> tier none", r.tier, "none")
check("no request issued", _install_openai.last_kwargs, None)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL LLM-EXTRACTION CHECKS PASSED")
