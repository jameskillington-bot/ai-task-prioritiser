"""Station reference data."""

# CRS codes for London stations. Journeys starting or ending at any of these
# count as "to or from London". Extend via `london_stations_extra` in config.
LONDON_CRS = frozenset({
    # Terminals
    "BFR", "CST", "CHX", "EUS", "FST", "KGX", "LBG", "LST", "MYB", "MOG",
    "PAD", "STP", "SPX", "VIC", "WAT", "WAE",
    # Central through stations
    "ZFD", "CTK", "OLD",
    # Major interchanges inside Greater London
    "SRA", "CLJ", "ECR", "WIJ", "HHY", "FPK", "RMF", "EAL", "WIM", "LEW",
    "SUR", "BAL", "EPH", "NWX", "HRW", "WMB", "KTN", "TOM",
})

# RTT searches by CRS. "London Terminals" group code on tickets maps to many.
LONDON_GROUP_CODES = frozenset({"ZLO", "LDN", "LONDON TERMINALS"})


def is_london(crs: str, extra: frozenset[str] = frozenset()) -> bool:
    code = crs.strip().upper()
    return code in LONDON_CRS or code in LONDON_GROUP_CODES or code in extra


# Display names for stations the agent commonly sees; anything else shows its code.
NAMES = {
    "AHT": "Aldershot", "WAT": "London Waterloo", "WAE": "London Waterloo East", "GLD": "Guildford",
    "WOK": "Woking", "FNB": "Farnborough (Main)", "ASV": "Ash Vale", "BKO": "Brookwood",
    "CLJ": "Clapham Junction", "VXH": "Vauxhall", "SUR": "Surbiton", "FNH": "Farnham",
    "BSK": "Basingstoke", "RDG": "Reading", "PAD": "London Paddington", "VIC": "London Victoria",
    "LBG": "London Bridge", "CHX": "London Charing Cross", "CST": "London Cannon Street",
    "BFR": "London Blackfriars", "EUS": "London Euston", "KGX": "London Kings Cross",
    "STP": "London St Pancras", "LST": "London Liverpool Street", "MYB": "London Marylebone",
    "FST": "London Fenchurch Street",
}


def name(crs: str) -> str:
    return NAMES.get(crs.upper(), crs.upper())
