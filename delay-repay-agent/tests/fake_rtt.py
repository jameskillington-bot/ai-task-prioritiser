"""An in-memory stand-in for Realtime Trains built from simple timetables."""

from datetime import datetime


def call(crs, arr=None, dep=None, rt_arr=None, rt_dep=None, cancelled=False, actual=True):
    loc = {"crs": crs, "displayAs": "CANCELLED_CALL" if cancelled else "CALL"}
    if arr:
        loc |= {"gbttBookedArrival": arr, "realtimeArrival": rt_arr or arr, "realtimeArrivalActual": actual}
    if dep:
        loc |= {"gbttBookedDeparture": dep, "realtimeDeparture": rt_dep or dep, "realtimeDepartureActual": actual}
    return loc


class FakeRtt:
    def __init__(self, run_date="2026-09-20"):
        self.run_date = run_date
        self.services = {}  # uid -> dict(atoc, locations)

    def add(self, uid, atoc, *locations):
        self.services[uid] = {"atocCode": atoc, "atocName": atoc + " Trains", "locations": list(locations)}

    def search(self, origin, destination, when):
        out = []
        for uid, s in self.services.items():
            crs = [l["crs"] for l in s["locations"]]
            if origin in crs and destination in crs and crs.index(origin) < crs.index(destination):
                o = s["locations"][crs.index(origin)]
                dep = datetime.strptime(self.run_date + o["gbttBookedDeparture"], "%Y-%m-%d%H%M")
                if when.replace(second=0) <= dep or abs((dep - when).total_seconds()) <= 3600:
                    if dep < when.replace(hour=min(23, when.hour + 2)):
                        out.append({"serviceUid": uid, "runDate": self.run_date, "atocCode": s["atocCode"],
                                    "atocName": s["atocName"], "locationDetail": o})
        return out

    def service(self, uid, run_date):
        return self.services[uid]
