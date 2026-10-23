"""app.main stays the one namespace for the stores after the routes moved to app/routes/ (see app/main.py)."""
from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.deliveries import DeliveryBook
from app.routes import common, desk


def test_each_shared_name_lives_in_one_route_module():
    """A store or helper defined in two of app/routes/* would make a swap on app.main reach only one of them."""
    owners: dict[str, str] = {}
    for m in main._HOLDERS:
        for node in ast.parse(Path(m.__file__).read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [n.id for t in targets for n in (t.elts if isinstance(t, ast.Tuple) else [t])
                         if isinstance(n, ast.Name)]
            else:
                continue
            for name in set(names) - {"router"}:  # each router module has its own, read as <module>.router
                assert name not in owners, f"{name} is defined in {owners[name]} and {m.__name__}"
                assert name not in vars(main), f"{name} is defined in app.main too, so a swap there stays there"
                owners[name] = m.__name__


def test_a_swap_on_app_main_reaches_the_routes(monkeypatch, tmp_path):
    book = DeliveryBook(tmp_path / "expected.json")
    monkeypatch.setattr(main, "book", book)
    assert main.book is book and common.book is book
    seen = []
    monkeypatch.setattr(main, "_record", lambda day: seen.append(day) or {"record": [], "perishable_at_risk": [],
                                                                         "month_money": None})
    assert desk._record is main._record
    assert TestClient(main.app).get("/?date=2026-10-07").status_code == 200 and seen
    monkeypatch.undo()
    assert common.book is not book and desk._record is not main.__dict__.get("_record")
