"""Train operators: compensation scheme, claim start page and trusted domains.

Operator codes are the ATOC codes that Realtime Trains reports for each
service. Claim URLs change often; the browser agent starts from `claim_url`
and navigates the operator's own site to find the form, so a stale deep link
still works as long as the domain is right. Override anything in config under
`operators:`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Operator:
    code: str
    name: str
    scheme: str  # "DR15" or "DR30"
    claim_url: str
    domains: tuple[str, ...] = field(default_factory=tuple)


_OPERATORS = [
    Operator("GW", "Great Western Railway", "DR15", "https://www.gwr.com/help-and-support/refunds-and-compensation/delay-repay", ("gwr.com",)),
    Operator("VT", "Avanti West Coast", "DR15", "https://www.avantiwestcoast.co.uk/help-and-support/delay-repay", ("avantiwestcoast.co.uk",)),
    Operator("GR", "LNER", "DR15", "https://www.lner.co.uk/support/refunds-and-compensation/delay-repay/", ("lner.co.uk",)),
    Operator("SE", "Southeastern", "DR15", "https://www.southeasternrailway.co.uk/delay-repay", ("southeasternrailway.co.uk",)),
    Operator("SN", "Southern", "DR15", "https://www.southernrailway.com/help-and-support/delay-repay", ("southernrailway.com",)),
    Operator("TL", "Thameslink", "DR15", "https://www.thameslinkrailway.com/help-and-support/delay-repay", ("thameslinkrailway.com",)),
    Operator("GN", "Great Northern", "DR15", "https://www.greatnorthernrail.com/help-and-support/delay-repay", ("greatnorthernrail.com",)),
    Operator("GX", "Gatwick Express", "DR15", "https://www.gatwickexpress.com/help-and-support/delay-repay", ("gatwickexpress.com",)),
    Operator("SW", "South Western Railway", "DR15", "https://www.southwesternrailway.com/contact-and-help/delay-repay", ("southwesternrailway.com",)),
    Operator("LE", "Greater Anglia", "DR15", "https://www.greateranglia.co.uk/about-us/our-performance/delay-repay", ("greateranglia.co.uk",)),
    Operator("CC", "c2c", "DR15", "https://www.c2c-online.co.uk/help-feedback/delay-repay/", ("c2c-online.co.uk",)),
    Operator("CH", "Chiltern Railways", "DR15", "https://www.chilternrailways.co.uk/delay-repay", ("chilternrailways.co.uk",)),
    Operator("EM", "East Midlands Railway", "DR15", "https://www.eastmidlandsrailway.co.uk/help-manage/delay-repay", ("eastmidlandsrailway.co.uk",)),
    Operator("XC", "CrossCountry", "DR15", "https://www.crosscountrytrains.co.uk/customer-service/delay-repay", ("crosscountrytrains.co.uk",)),
    Operator("LM", "London Northwestern Railway", "DR15", "https://www.londonnorthwesternrailway.co.uk/about-us/delay-repay", ("londonnorthwesternrailway.co.uk",)),
    Operator("HT", "Hull Trains", "DR15", "https://www.hulltrains.co.uk/delay-repay", ("hulltrains.co.uk",)),
    Operator("GC", "Grand Central", "DR15", "https://www.grandcentralrail.com/delay-repay", ("grandcentralrail.com",)),
    Operator("LD", "Lumo", "DR15", "https://www.lumo.co.uk/delay-repay", ("lumo.co.uk",)),
    Operator("HX", "Heathrow Express", "DR15", "https://www.heathrowexpress.com/help/delay-repay", ("heathrowexpress.com",)),
    Operator("TP", "TransPennine Express", "DR15", "https://www.tpexpress.co.uk/help/delay-repay", ("tpexpress.co.uk",)),
    Operator("NT", "Northern", "DR15", "https://www.northernrailway.co.uk/delay-repay", ("northernrailway.co.uk",)),
    Operator("AW", "Transport for Wales", "DR15", "https://tfw.wales/delay-repay", ("tfw.wales",)),
    Operator("CS", "Caledonian Sleeper", "DR30", "https://www.sleeper.scot/delay-repay", ("sleeper.scot",)),
    Operator("SR", "ScotRail", "DR30", "https://www.scotrail.co.uk/delay-repay", ("scotrail.co.uk",)),
]

OPERATORS: dict[str, Operator] = {op.code: op for op in _OPERATORS}

# Many operators host the form on a shared claims platform; these are allowed
# for every operator in addition to its own domains.
SHARED_CLAIM_DOMAINS: tuple[str, ...] = ()


def get_operator(code: str | None, overrides: dict | None = None) -> Operator | None:
    if not code:
        return None
    code = code.upper()
    base = OPERATORS.get(code)
    over = (overrides or {}).get(code)
    if base is None and over is None:
        return None
    if base is None:
        base = Operator(code, over.get("name", code), "DR15", over["claim_url"], ())
    if over:
        base = replace(
            base,
            name=over.get("name", base.name),
            scheme=over.get("scheme", base.scheme),
            claim_url=over.get("claim_url", base.claim_url),
            domains=tuple(base.domains) + tuple(over.get("extra_domains", ())),
        )
    return base
