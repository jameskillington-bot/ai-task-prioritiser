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
