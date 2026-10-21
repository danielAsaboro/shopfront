"""Which shop a Ring webhook belongs to, for the queue consumer (app/store/lambda_consumer.py).

One Ring partner app gets every linked account's webhooks at one URL, each carrying the owner's Ring
Account ID (`meta.account_id`) and the device (`data.attributes.source`) (API reference, Notifications).
The table is keyed by shop (`pk` = `<shop_id>#<UTC day>`, app/store/dynamodb.py), so the consumer has to
name the shop before it writes. It does that from a map held in config, never from anything in the request:

- Single shop (the default): the stack's ShopId, RingDeviceId and RingAccountId parameters, exactly the
  filter the app's local log applies (`app.ring.webhooks.ours`: an empty ID accepts any).
- Several shops: an SSM parameter (String or SecureString) named by the stack's ShopMapParameter, holding

      {"shops": [{"shop_id": "corner-bakery", "account_id": "<Ring Account ID>",
                  "device_ids": ["<doorbell device ID>", ...]}, ...]}

  `device_ids` may be empty or left out (every device on that account). With more than one shop every
  entry needs an `account_id`, and no event may match two shops, so a map that could misfile one shop's
  door into another's partition is refused outright rather than guessed at.

Standard library only: it is packaged into the Lambda with infra/package_lambda.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

SHOP_ID = re.compile(r"^[a-z0-9-]{1,40}$")  # the template's ShopId pattern; no "#", so a prefix is a partition


@dataclass(frozen=True)
class Shop:
    shop_id: str
    account_id: str = ""
    device_ids: tuple[str, ...] = ()

    def owns(self, row: dict) -> bool:
        return ((not self.account_id or row.get("account") == self.account_id)
                and (not self.device_ids or row.get("device") in self.device_ids))


class ShopMap:
    def __init__(self, shops: list[Shop]):
        if not shops:
            raise ValueError("the shop map names no shop")
        ids = [s.shop_id for s in shops]
        for shop_id in ids:
            if not SHOP_ID.match(shop_id):
                raise ValueError(f"shop_id {shop_id!r} must match {SHOP_ID.pattern}")
        if len(set(ids)) != len(ids):
            raise ValueError("a shop_id appears twice in the shop map")
        if len(shops) > 1:
            if any(not s.account_id for s in shops):
                raise ValueError("with more than one shop, every shop needs its Ring account_id")
            for i, a in enumerate(shops):
                for b in shops[i + 1:]:
                    if a.account_id == b.account_id and (not a.device_ids or not b.device_ids
                                                          or set(a.device_ids) & set(b.device_ids)):
                        raise ValueError(f"{a.shop_id} and {b.shop_id} could both claim the same doorbell: "
                                         "shops on one Ring account need disjoint device_ids")
        self.shops = list(shops)

    @classmethod
    def single(cls, shop_id: str, device_id: str = "", account_id: str = "") -> ShopMap:
        return cls([Shop(shop_id, account_id, (device_id,) if device_id else ())])

    @classmethod
    def parse(cls, text: str) -> ShopMap:
        try:
            entries = json.loads(text)["shops"]
            shops = [Shop(str(e["shop_id"]), str(e.get("account_id") or ""),
                          tuple(str(d) for d in e.get("device_ids") or ())) for e in entries]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"the shop map is not {{\"shops\": [{{\"shop_id\": ...}}]}}: {type(exc).__name__}")
        return cls(shops)

    def shop_for(self, row: dict) -> str | None:
        """The one shop this row belongs to, or None (not any shop's: dropped as IGNORED)."""
        found = [s.shop_id for s in self.shops if s.owns(row)]
        return found[0] if len(found) == 1 else None
