"""Property tests for the delivery matcher: random expected deliveries and random door visits,
checked against invariants a receipt must never break. Local only, no Ring or Bedrock."""

from datetime import date, datetime, time, timedelta
from itertools import permutations
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from app.deliveries import Expected, FrameStore, receipts
from app.models import ClassifiedVisit, DoorEvent, VisitType

TZ = ZoneInfo("Africa/Lagos")
DAY = date(2026, 10, 7)
OPEN = (time(8), time(18))
NO_FRAMES = FrameStore.__new__(FrameStore)
NO_FRAMES.dirs = []

minutes = st.integers(min_value=0, max_value=24 * 60 - 1)


@st.composite
def expected_list(draw):
    out = []
    for i in range(draw(st.integers(0, 5))):
        if draw(st.booleans()):
            a, b = sorted(draw(st.lists(minutes, min_size=2, max_size=2, unique=True)))
            ws, we = f"{a // 60:02d}:{a % 60:02d}", f"{b // 60:02d}:{b % 60:02d}"
        else:
            ws = we = None
        out.append(Expected(f"exp-{i}", DAY.isoformat(), f"Supplier {i}", ws, we))
    return sorted(out, key=lambda e: (e.window_start or "99", e.supplier.lower()))


@st.composite
def visit_list(draw):
    out = []
    for i, m in enumerate(draw(st.lists(minutes, max_size=6))):
        kind = draw(st.sampled_from([VisitType.DELIVERY, VisitType.DELIVERY, VisitType.CUSTOMER]))
        ev = DoorEvent(id=f"ev-{i}", device_id="d", kind="ding", source="ring",
                       timestamp=datetime.combine(DAY, time(m // 60, m % 60), tzinfo=TZ))
        out.append(ClassifiedVisit(ev, kind, 0.9, "", "bedrock"))
    return out


def in_window(v: ClassifiedVisit, e: Expected) -> bool:
    w = e.window()
    t = v.event.timestamp.time()
    return w is None or w[0] <= t <= w[1]


def best_possible_in_window(expected, couriers) -> int:
    """Brute force: the most expected deliveries that could be matched inside their window."""
    best = 0
    slots = couriers + [None] * len(expected)
    for perm in permutations(range(len(slots)), len(expected)):
        n = sum(1 for e, j in zip(expected, perm) if slots[j] is not None and in_window(slots[j], e))
        best = max(best, n)
    return best


@settings(max_examples=400, deadline=None)
@given(expected_list(), visit_list())
def test_matcher_invariants(expected, visits):
    r = receipts(expected, visits, OPEN, NO_FRAMES)
    couriers = [v for v in visits if v.visit_type == VisitType.DELIVERY]
    used = [x["visit"]["event_id"] for x in r["expected"] if x["visit"]]

    # one row per expected delivery, and one door visit never backs two receipts
    assert [x["id"] for x in r["expected"]] == [e.id for e in expected]
    assert len(used) == len(set(used))
    # only courier-labelled visits become receipts, and every courier is accounted for exactly once
    assert set(used) <= {v.event.id for v in couriers}
    assert sorted(used + [u["event_id"] for u in r["unexpected"]]) == sorted(v.event.id for v in couriers)
    # no supplier is reported "not seen" while an unmatched courier is standing by
    if len(couriers) >= len(expected):
        assert all(x["status"] != "not_seen" for x in r["expected"])
    # the matcher finds as many in-window receipts as any assignment could
    got = sum(1 for x in r["expected"] if x["visit"] and "outside_window" not in x["status"])
    assert got == best_possible_in_window(expected, couriers)


def test_nested_windows_both_land_inside():
    """Found by the property test: greedy matching gave the wide window the early courier and pushed
    the narrow window's supplier outside its window."""
    wide = Expected("exp-a", DAY.isoformat(), "A", "09:00", "12:00")
    narrow = Expected("exp-b", DAY.isoformat(), "B", "10:00", "11:00")
    vs = []
    for i, (h, m) in enumerate([(10, 30), (11, 30)]):
        ev = DoorEvent(f"ev-{i}", "d", datetime.combine(DAY, time(h, m), tzinfo=TZ), "ding", "ring")
        vs.append(ClassifiedVisit(ev, VisitType.DELIVERY, 0.9, "", "bedrock"))
    r = receipts([wide, narrow], vs, OPEN, NO_FRAMES)
    assert [x["status"] for x in r["expected"]] == ["seen", "seen"]
    assert {x["supplier"]: x["visit"]["time"] for x in r["expected"]} == {"A": "11:30", "B": "10:30"}


@settings(max_examples=200, deadline=None)
@given(expected_list(), visit_list(), st.randoms())
def test_matcher_ignores_the_order_events_arrive_in(expected, visits, rnd):
    """Webhooks and history pages can arrive out of order; the receipts must not change."""
    shuffled = visits[:]
    rnd.shuffle(shuffled)
    a, b = receipts(expected, visits, OPEN, NO_FRAMES), receipts(expected, shuffled, OPEN, NO_FRAMES)
    key = lambda r: [(x["id"], x["status"], x["visit"] and x["visit"]["time"]) for x in r["expected"]]
    assert key(a) == key(b)
