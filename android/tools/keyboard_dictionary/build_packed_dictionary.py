#!/usr/bin/env python3
"""Build Aura's deterministic packed radix dictionary asset.

The input is the licensed ``word frequency`` text file in app assets. The output format is
read-only, little-endian, and deliberately simple enough for the Android IME to memory-map and
validate without allocating a graph of Kotlin objects.

Every packed node stores a bounded list of its highest-frequency descendant words. A prefix
lookup therefore traverses only the compressed radix labels and then decodes at most K words;
it never scans the full matching prefix range.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path


MAGIC = b"AURAPD01"
VERSION = 1
MAX_TOP = 8
HEADER = struct.Struct("<8s8I32s")
NODE = struct.Struct("<5i")
EDGE = struct.Struct("<4i")
WORD = struct.Struct("<3i")
VALID_WORD = re.compile(r"[a-z]+")


@dataclass
class TrieNode:
    children: dict[str, "TrieNode"] = field(default_factory=dict)
    terminal_word_id: int = -1


@dataclass
class RadixEdge:
    label: str
    child: "RadixNode"


@dataclass
class RadixNode:
    terminal_word_id: int = -1
    edges: list[RadixEdge] = field(default_factory=list)
    top_word_ids: list[int] = field(default_factory=list)
    index: int = -1


def read_entries(path: Path) -> list[tuple[str, int]]:
    by_word: dict[str, int] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = raw.split()
        if len(parts) != 2 or not VALID_WORD.fullmatch(parts[0]):
            raise ValueError(f"invalid dictionary row {line_number}: {raw!r}")
        frequency = int(parts[1])
        if frequency < 0:
            raise ValueError(f"negative frequency on row {line_number}")
        word = parts[0]
        by_word[word] = max(by_word.get(word, 0), frequency)
    return sorted(by_word.items())


def build_trie(entries: list[tuple[str, int]]) -> TrieNode:
    root = TrieNode()
    for word_id, (word, _frequency) in enumerate(entries):
        node = root
        for character in word:
            node = node.children.setdefault(character, TrieNode())
        node.terminal_word_id = word_id
    return root


def compress(node: TrieNode) -> RadixNode:
    packed = RadixNode(terminal_word_id=node.terminal_word_id)
    for character, child in sorted(node.children.items()):
        label = character
        while child.terminal_word_id < 0 and len(child.children) == 1:
            next_character, next_child = next(iter(child.children.items()))
            label += next_character
            child = next_child
        packed.edges.append(RadixEdge(label=label, child=compress(child)))
    return packed


def populate_top(node: RadixNode, entries: list[tuple[str, int]]) -> list[int]:
    candidates: list[int] = []
    if node.terminal_word_id >= 0:
        candidates.append(node.terminal_word_id)
    for edge in node.edges:
        candidates.extend(populate_top(edge.child, entries))
    candidates.sort(key=lambda word_id: (-entries[word_id][1], entries[word_id][0]))
    node.top_word_ids = candidates[:MAX_TOP]
    return node.top_word_ids


def assign_indices(root: RadixNode) -> list[RadixNode]:
    ordered: list[RadixNode] = []

    def visit(node: RadixNode) -> None:
        node.index = len(ordered)
        ordered.append(node)
        for edge in node.edges:
            visit(edge.child)

    visit(root)
    return ordered


def build_bytes(source: Path) -> bytes:
    entries = read_entries(source)
    root = compress(build_trie(entries))
    populate_top(root, entries)
    nodes = assign_indices(root)

    edge_rows: list[tuple[int, int, int, int]] = []
    top_ids: list[int] = []
    label_blob = bytearray()
    node_rows: list[tuple[int, int, int, int, int]] = []

    for node in nodes:
        first_edge = len(edge_rows)
        for edge in node.edges:
            encoded = edge.label.encode("ascii")
            label_offset = len(label_blob)
            label_blob.extend(encoded)
            edge_rows.append((label_offset, len(encoded), edge.child.index, encoded[0]))
        top_start = len(top_ids)
        top_ids.extend(node.top_word_ids)
        node_rows.append(
            (
                first_edge,
                len(node.edges),
                top_start,
                len(node.top_word_ids),
                node.terminal_word_id,
            )
        )

    word_blob = bytearray()
    word_rows: list[tuple[int, int, int]] = []
    for word, frequency in entries:
        encoded = word.encode("ascii")
        offset = len(word_blob)
        word_blob.extend(encoded)
        word_rows.append((offset, len(encoded), frequency))

    source_hash = hashlib.sha256(source.read_bytes()).digest()
    output = bytearray(
        HEADER.pack(
            MAGIC,
            VERSION,
            MAX_TOP,
            len(node_rows),
            len(edge_rows),
            len(word_rows),
            len(top_ids),
            len(label_blob),
            len(word_blob),
            source_hash,
        )
    )
    for row in node_rows:
        output.extend(NODE.pack(*row))
    for row in edge_rows:
        output.extend(EDGE.pack(*row))
    for word_id in top_ids:
        output.extend(struct.pack("<i", word_id))
    output.extend(label_blob)
    for row in word_rows:
        output.extend(WORD.pack(*row))
    output.extend(word_blob)
    return bytes(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    packed = build_bytes(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(packed)
    print(f"wrote {args.output} ({len(packed)} bytes, sha256={hashlib.sha256(packed).hexdigest()})")


if __name__ == "__main__":
    main()
