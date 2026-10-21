"""Drawn stills for demo door events: what a frame would show, never a frame.

Synthetic events (and the demo feeder's replayed webhooks) have no camera frame, so the receiving desk and
the dispute pack would show no picture at all. These SVG drawings fill the same slots a Ring frame would,
generated here in code, and every one says on itself, in its alt text and in the caption beside it:
"Illustration, not a camera frame". They are never Ring frames, never Bedrock evidence, never in a pack's
zip, and never shown for an event from a real Ring doorbell (one with no frame shows no picture).

A scene follows the event's label, so a rules guess draws as neutrally as it is worded: a doorbell press
nothing looked at is an empty doorway with the bell, not a courier.
"""

from __future__ import annotations

from functools import lru_cache

CAPTION = "Illustration, not a camera frame"

# Event sources that are Shopfront's own demo data: the only ones that ever get a drawing
DEMO_SOURCES = ("synthetic", "ring-webhook-replay")

SCENES = {
    "courier": "a courier with a parcel at the shop door",
    "customer": "a customer at the shop door",
    "staff": "a member of staff with the keys at the shop door",
    "night": "the empty doorstep at night",
    "doorway": "the shop doorway, nobody identified",
}

# Visit label -> scene. Anything not here (doorbell_press, unknown) is the empty doorway.
_BY_TYPE = {"delivery": "courier", "customer": "customer", "staff": "staff", "after_hours": "night"}


def scene_for(visit_type: str | None) -> str:
    return _BY_TYPE.get(visit_type or "", "doorway")


def url_for(row: dict | None, visit_type: str | None = None) -> str | None:
    """The drawing for a visit row (deliveries._visit_row, dispute._timeline, ...), or None when the event
    isn't demo data or already has a frame. `visit_type` overrides the row's own `type`."""
    row = row or {}
    if row.get("frame_url") or row.get("event_source") not in DEMO_SOURCES:
        return None
    scene = scene_for(visit_type or row.get("type"))
    if scene == "customer":  # the same event always gets the same customer
        n = sum(map(ord, str(row.get("event_id") or row.get("time") or ""))) % len(_CUSTOMERS)
        return f"/illustrations/customer{f'-{n + 1}' if n else ''}.svg"
    return f"/illustrations/{scene}.svg"


def files() -> dict[str, str]:
    """Every drawing the app serves, by file stem: the scenes, plus the other customers."""
    return {**{s: s for s in SCENES}, **{f"customer-{i + 1}": "customer" for i in range(1, len(_CUSTOMERS))}}


def alt_for(visit_type: str | None) -> str:
    return f"{CAPTION}: {SCENES[scene_for(visit_type)]}"


# --- drawing -------------------------------------------------------------------------------------------

_STYLE = """<style>
  .wall{fill:#ece8e0} .sign{fill:#d9d3c7} .frame{fill:#8a8780} .door{fill:#c9c2b4} .glass{fill:#e4eaee}
  .shelf{fill:#d4c4a8} .step{fill:#b9b3a7} .ground{fill:#dcd7cd} .bell{fill:#1d1d1f} .plant{fill:#6f8a62}
  .pot{fill:#a8775a} .ink{fill:#2b2b30} .skin{fill:#d8b48f} .skin2{fill:#a97c58} .hair{fill:#3a2e28}
  .band{fill:#1d1d1f;fill-opacity:.8} .cap{fill:#ffffff;font:600 12px -apple-system,'Segoe UI',sans-serif}
  .parcel{fill:#b98a55} .tape{fill:#e8d3b0} .coat-a{fill:#3e5a7a} .coat-b{fill:#7a4e5a} .coat-c{fill:#4f6a4f}
  .apron{fill:#f4f1ea} .hat{fill:#2f3f55} .key{fill:none;stroke:#a07f35;stroke-width:2;stroke-linecap:round}
  .bag{fill:#c9a66b} .strap{fill:none;stroke:#8a6d2f;stroke-width:2} .ring{fill:none;stroke:#8a8780}
  @media (prefers-color-scheme: dark){
    .wall{fill:#2a2a2e} .sign{fill:#34343a} .frame{fill:#5a5a62} .door{fill:#3d3d44} .glass{fill:#46515c}
    .shelf{fill:#5a5348} .step{fill:#45454c} .ground{fill:#303035} .apron{fill:#d9d6cf} .bell{fill:#ececee}
    .ring{stroke:#9a9aa1} .ink{fill:#0e0e11}
  }
</style>"""

NIGHT = {"wall": "#252b39", "sign": "#2f3647", "frame": "#4a5163", "door": "#323a4b",
         "glass": "#2b3446", "shelf": "#3a3a40", "ground": "#20252f", "step": "#2c3240"}


def _person(x: int, coat: str, hair: str = "", hold: str = "", extra: str = "", skin: str = "skin") -> str:
    """A simple standing figure, feet on the ground line at y=206. `hold` replaces the arms at the sides."""
    arms = hold or (f'<rect x="-29" y="110" width="11" height="50" rx="5" class="{coat}"/>'
                    f'<rect x="18" y="110" width="11" height="50" rx="5" class="{coat}"/>'
                    f'<circle cx="-23.5" cy="161" r="5" class="{skin}"/><circle cx="23.5" cy="161" r="5" class="{skin}"/>')
    return (f'<g transform="translate({x} 0)">'
            f'<rect x="-14" y="166" width="11" height="40" rx="4" class="ink"/>'
            f'<rect x="3" y="166" width="11" height="40" rx="4" class="ink"/>'
            f'<rect x="-20" y="104" width="40" height="68" rx="13" class="{coat}"/>'
            f'<rect x="-5" y="96" width="10" height="10" class="{skin}"/>'
            f'<circle cx="0" cy="84" r="15" class="{skin}"/>{hair}{extra}{arms}</g>')


def _shop(night: bool = False) -> str:
    n = NIGHT if night else {}

    def c(name: str) -> str:  # a class by day; a fixed colour at night (the night is dark in both themes)
        return f'fill="{n[name]}"' if night else f'class="{name}"'
    wins = "".join(f'<rect x="{x}" y="70" width="{w}" height="60" rx="2" {c("glass")}/>'
                   f'<rect x="{x}" y="116" width="{w}" height="4" {c("shelf")}/>' for x, w in ((24, 64), (216, 80)))
    bell = 'fill="#9fb4d6"' if night else 'class="bell"'
    goods = "" if night else ('<circle cx="234" cy="110" r="6" class="shelf"/><circle cx="250" cy="110" r="6" '
                              'class="shelf"/><rect x="262" y="104" width="22" height="12" rx="6" class="shelf"/>'
                              '<rect x="36" y="102" width="18" height="14" rx="3" class="shelf"/>')
    return (f'<rect width="320" height="220" {c("wall")}/>'
            f'<rect x="40" y="30" width="240" height="22" rx="3" {c("sign")}/>' + wins + goods +
            f'<rect x="96" y="58" width="104" height="150" {c("frame")}/>'
            f'<rect x="102" y="64" width="92" height="144" {c("door")}/>'
            f'<rect x="112" y="74" width="72" height="84" rx="2" {c("glass")}/>'
            f'<rect x="174" y="150" width="4" height="22" rx="2" {c("frame")}/>'
            f'<rect x="207" y="122" width="10" height="14" rx="3" {c("frame")}/>'
            f'<circle cx="212" cy="129" r="3" {bell}/>'
            f'<rect x="0" y="206" width="320" height="34" {c("ground")}/>'
            f'<rect x="88" y="206" width="120" height="8" {c("step")}/>'
            '<path d="M58 206 l-6 -24 h28 l-6 24 z" class="pot"/>'
            '<ellipse cx="66" cy="170" rx="16" ry="14" class="plant"/>')


def _caption() -> str:
    return (f'<rect x="0" y="216" width="320" height="24" class="band"/>'
            f'<text x="160" y="232" text-anchor="middle" class="cap">{CAPTION}</text>')


def _courier() -> str:
    hold = ('<rect x="-30" y="112" width="11" height="34" rx="5" class="coat-a" transform="rotate(-25 -24 112)"/>'
            '<rect x="19" y="112" width="11" height="34" rx="5" class="coat-a" transform="rotate(25 24 112)"/>'
            '<rect x="-24" y="128" width="48" height="36" rx="2" class="parcel"/>'
            '<rect x="-24" y="141" width="48" height="7" class="tape"/>'
            '<circle cx="-24" cy="146" r="5" class="skin"/><circle cx="24" cy="146" r="5" class="skin"/>')
    hat = ('<path d="M-16 80 a16 15 0 0 1 32 0 z" class="hat"/>'
           '<rect x="2" y="76" width="22" height="5" rx="2" class="hat"/>')
    return _shop() + _person(244, "coat-a", hat, hold)


# A few different customers, so a busy hour isn't six copies of one person: (coat, skin, hair, bag, x)
_LONG_HAIR = ('<path d="M-16 86 a16 17 0 0 1 32 -4 q-12 -6 -24 -2 q-5 3 -8 6 z" class="hair"/>'
              '<path d="M-16 84 q-2 16 6 22 h-4 q-6 -8 -2 -22 z" class="hair"/>')
_SHORT_HAIR = '<path d="M-15 82 a15 15 0 0 1 30 0 q-8 -6 -15 -3 q-8 -3 -15 3 z" class="hair"/>'
_BEANIE = '<path d="M-16 82 a16 16 0 0 1 32 0 z" class="coat-c"/><rect x="-17" y="79" width="34" height="5" rx="2" class="coat-c"/>'
_TOTE = ('<path d="M22 146 q7 -14 14 0" class="strap"/>'
         '<rect x="18" y="146" width="22" height="26" rx="3" class="bag"/>')
_CUSTOMERS = [("coat-b", "skin2", _LONG_HAIR, _TOTE, 250), ("coat-a", "skin", _SHORT_HAIR, "", 236),
              ("coat-c", "skin2", _BEANIE, _TOTE, 258)]


def _customer(variant: int = 0) -> str:
    coat, skin, hair, bag, x = _CUSTOMERS[variant % len(_CUSTOMERS)]
    return _shop() + _person(x, coat, hair, extra=bag, skin=skin)


def _staff() -> str:
    hair = '<path d="M-15 82 a15 15 0 0 1 30 0 q-8 -6 -15 -3 q-8 -3 -15 3 z" class="hair"/>'
    apron = ('<rect x="-14" y="120" width="28" height="48" rx="4" class="apron"/>'
             '<path d="M-10 120 l10 -14 l10 14" fill="none" stroke="#c9c2b4" stroke-width="2"/>')
    hold = ('<rect x="-29" y="110" width="11" height="50" rx="5" class="coat-c"/>'
            '<circle cx="-23.5" cy="161" r="5" class="skin"/>'
            '<rect x="18" y="112" width="11" height="34" rx="5" class="coat-c" transform="rotate(-35 23 112)"/>'
            '<circle cx="37" cy="138" r="5" class="skin"/>'
            '<circle cx="42" cy="146" r="5" class="key"/><path d="M42 151 v11 m0 -5 h4 m-4 4 h3" class="key"/>')
    return _shop() + _person(134, "coat-c", hair, hold, apron)


def _night() -> str:
    return (_shop(night=True) +
            '<path d="M128 52 h40 l-6 8 h-28 z" fill="#4a5163"/>'
            '<path d="M134 60 h28 l40 146 h-108 z" fill="#f3d98b" fill-opacity=".10"/>'
            '<ellipse cx="148" cy="208" rx="64" ry="7" fill="#f3d98b" fill-opacity=".35"/>')


def _doorway() -> str:
    return _shop() + ('<circle cx="212" cy="129" r="11" class="ring" stroke-width="1.5"/>'
                      '<circle cx="212" cy="129" r="18" class="ring" stroke-width="1" stroke-opacity=".6"/>')


_DRAW = {"courier": _courier, "customer": _customer, "staff": _staff, "night": _night, "doorway": _doorway}


@lru_cache(maxsize=None)
def svg(name: str) -> str:
    """The drawing for a file stem from files(), as a standalone SVG document. KeyError for one not drawn."""
    scene = files()[name]
    body = _customer(int(name.rsplit("-", 1)[1]) - 1) if name != scene else _DRAW[scene]()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 240" width="320" height="240" role="img" '
            f'aria-label="{CAPTION}: {SCENES[scene]}"><title>{CAPTION}: {SCENES[scene]}</title>{_STYLE}'
            f'{body}{_caption()}</svg>')


# The tab icon: a shop door with its bell, ink on the page colour (and the reverse in dark mode)
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><style>.b{fill:#1d1d1f}.f{fill:#f7f6f3}'
           '@media (prefers-color-scheme: dark){.b{fill:#ececee}.f{fill:#151517}}</style>'
           '<rect width="32" height="32" rx="7" class="b"/><rect x="9" y="6" width="12" height="21" rx="1.5" class="f"/>'
           '<rect x="11.5" y="8.5" width="7" height="8" rx="1" class="b"/><circle cx="25" cy="16" r="2" class="f"/></svg>')
