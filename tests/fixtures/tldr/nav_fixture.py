"""Fixture for .claude/hooks/test_tldr_read.sh: a stable, in-repo code file above
the hook's 1500-byte threshold, with imports, functions and classes, so the
tldr nav-map, cache, shim and launch-dir cases run without a live ~/.claude
install. Never imported; only `tldr extract` reads it. Keep it well above 1500 bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Item:
    """One stock-keeping unit with a quantity and a unit price."""

    sku: str
    name: str
    quantity: int = 0
    unit_price: float = 0.0
    tags: list[str] = field(default_factory=list)

    def total_value(self) -> float:
        """Quantity times unit price."""
        return self.quantity * self.unit_price

    def has_tag(self, tag: str) -> bool:
        """True when the item carries the given tag (case-insensitive)."""
        return tag.lower() in (t.lower() for t in self.tags)


class Inventory:
    """An in-memory collection of items keyed by SKU."""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}

    def add(self, item: Item) -> None:
        """Insert or replace an item."""
        self._items[item.sku] = item

    def remove(self, sku: str) -> Item | None:
        """Drop an item; returns it, or None when the SKU is unknown."""
        return self._items.pop(sku, None)

    def get(self, sku: str) -> Item | None:
        """Look up an item by SKU."""
        return self._items.get(sku)

    def restock(self, sku: str, amount: int) -> int:
        """Increase the quantity of an item; returns the new quantity."""
        if amount < 0:
            raise ValueError("restock amount must be non-negative")
        item = self._items[sku]
        item.quantity += amount
        return item.quantity

    def consume(self, sku: str, amount: int) -> int:
        """Decrease the quantity of an item; refuses to go below zero."""
        item = self._items[sku]
        if amount > item.quantity:
            raise ValueError(f"only {item.quantity} of {sku} in stock")
        item.quantity -= amount
        return item.quantity

    def total_value(self) -> float:
        """Sum of every item's value."""
        return sum(item.total_value() for item in self._items.values())

    def low_stock(self, threshold: int = 5) -> list[Item]:
        """Items at or below the threshold, lowest quantity first."""
        low = [i for i in self._items.values() if i.quantity <= threshold]
        return sorted(low, key=lambda i: (i.quantity, i.sku))

    def by_tag(self, tag: str) -> list[Item]:
        """Items carrying a tag, in SKU order."""
        return sorted(
            (i for i in self._items.values() if i.has_tag(tag)), key=lambda i: i.sku
        )

    def __len__(self) -> int:
        return len(self._items)


def load_inventory(path: Path) -> Inventory:
    """Read an inventory from a JSON list of item objects."""
    inventory = Inventory()
    for raw in json.loads(path.read_text(encoding="utf-8")):
        inventory.add(Item(**raw))
    return inventory


def save_inventory(inventory: Inventory, path: Path) -> None:
    """Write an inventory as a JSON list, sorted by SKU."""
    rows = [vars(inventory.get(sku)) for sku in sorted(inventory._items)]
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def format_report(inventory: Inventory, threshold: int = 5) -> str:
    """Plain-text summary: item count, total value and low-stock lines."""
    lines = [
        f"items: {len(inventory)}",
        f"total value: {inventory.total_value():.2f}",
    ]
    for item in inventory.low_stock(threshold):
        lines.append(f"low: {item.sku} {item.name} ({item.quantity})")
    return "\n".join(lines)


def merge(a: Inventory, b: Inventory) -> Inventory:
    """Combine two inventories; quantities add up when a SKU is in both."""
    merged = Inventory()
    for source in (a, b):
        for sku, item in source._items.items():
            existing = merged.get(sku)
            if existing is None:
                merged.add(Item(item.sku, item.name, item.quantity, item.unit_price))
            else:
                existing.quantity += item.quantity
    return merged
