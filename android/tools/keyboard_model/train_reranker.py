#!/usr/bin/env python3
"""Train and export Aura's compact local keyboard candidate reranker.

The only corpus input is the checked-in MIT-licensed FrequencyWords derivative used by the
deterministic dictionary. Training examples are deterministic synthetic typo/candidate groups;
no user text is collected or consumed. The model augments the lexical engine and never generates
tokens or decides whether a key is committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path

import numpy as np
import onnx
import onnxruntime
from onnx import TensorProto, helper, numpy_helper
from onnxruntime.quantization import QuantType, quantize_dynamic


SEED = 2_026_081_5
MAX_CANDIDATES = 8
FEATURE_COUNT = 8
HIDDEN_COUNT = 12
PARAMETER_COUNT = FEATURE_COUNT * HIDDEN_COUNT + HIDDEN_COUNT + HIDDEN_COUNT + 1
FEATURE_SCHEMA = (
    "lexical_rank,log_frequency,common_prefix,edit_similarity,"
    "keyboard_proximity,length_similarity,personal_source,next_word"
)
QWERTY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def read_entries(path: Path) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        word, frequency = raw.split()
        if word.isascii() and word.isalpha() and 3 <= len(word) <= 16:
            rows.append((word, int(frequency)))
    return rows


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, a in enumerate(left, 1):
        current = [row]
        for column, b in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (a != b)))
        previous = current
    return previous[-1]


def common_prefix(left: str, right: str) -> int:
    count = 0
    for a, b in zip(left, right):
        if a != b:
            break
        count += 1
    return count


def positions() -> dict[str, tuple[int, int]]:
    output: dict[str, tuple[int, int]] = {}
    for y, row in enumerate(QWERTY_ROWS):
        offset = y
        for index, character in enumerate(row):
            output[character] = (offset + index * 2, y)
    return output


POSITIONS = positions()


def proximity(source: str, candidate: str) -> int:
    score = 0
    for a, b in zip(source, candidate):
        if a == b or a not in POSITIONS or b not in POSITIONS:
            continue
        ax, ay = POSITIONS[a]
        bx, by = POSITIONS[b]
        if (ax - bx) ** 2 + 4 * (ay - by) ** 2 <= 8:
            score += 2
    for index in range(min(len(source), len(candidate)) - 1):
        if source[index] == candidate[index + 1] and source[index + 1] == candidate[index]:
            score += 3
    return score


def make_typo(word: str, rng: random.Random) -> str:
    operation = rng.randrange(3)
    index = rng.randrange(len(word) - (1 if operation == 2 else 0))
    if operation == 0:
        return word[:index] + word[index + 1:]
    if operation == 1:
        position = POSITIONS[word[index]]
        neighbors = [char for char, other in POSITIONS.items()
                     if char != word[index] and
                     (position[0] - other[0]) ** 2 + 4 * (position[1] - other[1]) ** 2 <= 8]
        replacement = rng.choice(neighbors) if neighbors else "e"
        return word[:index] + replacement + word[index + 1:]
    return word[:index] + word[index + 1] + word[index] + word[index + 2:]


def features(raw: str, candidate: str, frequency: int, rank: int,
             maximum_log_frequency: float, personal: bool, next_word: bool) -> list[float]:
    distance = min(edit_distance(raw, candidate), 3) if raw else 3
    prefix_denominator = max(1, min(len(raw), len(candidate)))
    return [
        1.0 - rank / (MAX_CANDIDATES - 1),
        math.log1p(frequency) / maximum_log_frequency,
        common_prefix(raw, candidate) / prefix_denominator if raw else 0.0,
        1.0 - distance / 3.0 if raw else 0.0,
        min(proximity(raw, candidate), 8) / 8.0 if raw else 0.0,
        1.0 - min(abs(len(raw) - len(candidate)), 8) / 8.0 if raw else 0.0,
        1.0 if personal else 0.0,
        1.0 if next_word else 0.0,
    ]


def make_groups(entries: list[tuple[str, int]], count: int,
                rng: random.Random) -> tuple[np.ndarray, np.ndarray]:
    maximum_log_frequency = math.log1p(max(frequency for _, frequency in entries))
    by_initial: dict[str, list[int]] = {}
    for index, (word, _) in enumerate(entries):
        by_initial.setdefault(word[0], []).append(index)
    inputs = np.zeros((count, MAX_CANDIDATES, FEATURE_COUNT), dtype=np.float32)
    labels = np.zeros(count, dtype=np.int64)
    eligible = [index for index, (word, _) in enumerate(entries) if len(by_initial[word[0]]) >= 8]
    for group in range(count):
        target_index = rng.choice(eligible)
        target_word, _ = entries[target_index]
        next_word = rng.random() < 0.18
        raw = "" if next_word else make_typo(target_word, rng)
        pool = by_initial[target_word[0]].copy()
        rng.shuffle(pool)
        distractors = [index for index in pool if index != target_index][:MAX_CANDIDATES - 1]
        candidates = [target_index, *distractors]
        rng.shuffle(candidates)
        correct_rank = candidates.index(target_index)
        labels[group] = correct_rank
        for rank, candidate_index in enumerate(candidates):
            candidate, frequency = entries[candidate_index]
            personal = candidate_index == target_index and rng.random() < 0.10
            inputs[group, rank] = features(
                raw, candidate, frequency, rank, maximum_log_frequency, personal, next_word,
            )
    return inputs, labels


def train(inputs: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, ...]:
    rng = np.random.default_rng(SEED)
    w1 = (rng.standard_normal((FEATURE_COUNT, HIDDEN_COUNT)) * 0.08).astype(np.float32)
    b1 = np.zeros(HIDDEN_COUNT, dtype=np.float32)
    w2 = (rng.standard_normal((HIDDEN_COUNT, 1)) * 0.08).astype(np.float32)
    b2 = np.zeros(1, dtype=np.float32)
    learning_rate = 0.035
    batch_size = 64
    for _epoch in range(55):
        for start in range(0, len(inputs), batch_size):
            x = inputs[start:start + batch_size]
            y = labels[start:start + batch_size]
            hidden_pre = x @ w1 + b1
            hidden = np.maximum(hidden_pre, 0)
            logits = (hidden @ w2 + b2).squeeze(-1)
            logits -= logits.max(axis=1, keepdims=True)
            probabilities = np.exp(logits)
            probabilities /= probabilities.sum(axis=1, keepdims=True)
            probabilities[np.arange(len(y)), y] -= 1
            gradient_logits = probabilities / len(y)
            grad_w2 = hidden.reshape(-1, HIDDEN_COUNT).T @ gradient_logits.reshape(-1, 1)
            grad_b2 = np.array([gradient_logits.sum()], dtype=np.float32)
            grad_hidden = gradient_logits[..., None] * w2[:, 0]
            grad_hidden[hidden_pre <= 0] = 0
            grad_w1 = x.reshape(-1, FEATURE_COUNT).T @ grad_hidden.reshape(-1, HIDDEN_COUNT)
            grad_b1 = grad_hidden.sum(axis=(0, 1))
            w1 -= learning_rate * grad_w1
            b1 -= learning_rate * grad_b1
            w2 -= learning_rate * grad_w2
            b2 -= learning_rate * grad_b2
        learning_rate *= 0.97
    return w1, b1, w2, b2


def predict(inputs: np.ndarray, weights: tuple[np.ndarray, ...]) -> np.ndarray:
    w1, b1, w2, b2 = weights
    return (np.maximum(inputs @ w1 + b1, 0) @ w2 + b2).squeeze(-1)


def export_float(path: Path, weights: tuple[np.ndarray, ...], metadata: dict[str, str]) -> None:
    w1, b1, w2, b2 = weights
    graph = helper.make_graph(
        [
            helper.make_node("MatMul", ["features", "w1"], ["hidden_linear"]),
            helper.make_node("Add", ["hidden_linear", "b1"], ["hidden_biased"]),
            helper.make_node("Relu", ["hidden_biased"], ["hidden"]),
            helper.make_node("MatMul", ["hidden", "w2"], ["score_3d"]),
            helper.make_node("Add", ["score_3d", "b2"], ["score_biased"]),
            helper.make_node("Reshape", ["score_biased", "output_shape"], ["scores"]),
        ],
        "aura_keyboard_candidate_reranker",
        [helper.make_tensor_value_info(
            "features", TensorProto.FLOAT, [1, MAX_CANDIDATES, FEATURE_COUNT],
        )],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [1, MAX_CANDIDATES])],
        [
            numpy_helper.from_array(w1, "w1"), numpy_helper.from_array(b1, "b1"),
            numpy_helper.from_array(w2, "w2"), numpy_helper.from_array(b2, "b2"),
            numpy_helper.from_array(np.array([1, MAX_CANDIDATES], dtype=np.int64), "output_shape"),
        ],
    )
    model = helper.make_model(graph, producer_name="Aura keyboard model builder",
                              opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    for key, value in sorted(metadata.items()):
        property_entry = model.metadata_props.add()
        property_entry.key = key
        property_entry.value = value
    onnx.checker.check_model(model)
    onnx.save_model(model, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("provenance", type=Path)
    args = parser.parse_args()
    entries = read_entries(args.source)
    source_sha = hashlib.sha256(args.source.read_bytes()).hexdigest()
    license_path = args.source.with_name("LICENSE")
    if not license_path.is_file():
        raise FileNotFoundError(f"corpus license file missing: {license_path}")
    license_sha = hashlib.sha256(license_path.read_bytes()).hexdigest()
    builder_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    random_source = random.Random(SEED)
    train_inputs, train_labels = make_groups(entries, 4_096, random_source)
    validation_inputs, validation_labels = make_groups(entries, 1_024, random_source)
    weights = train(train_inputs, train_labels)
    validation_scores = predict(validation_inputs, weights)
    top1 = float(np.mean(validation_scores.argmax(axis=1) == validation_labels))
    top3 = float(np.mean([
        label in np.argpartition(-row, 3)[:3]
        for row, label in zip(validation_scores, validation_labels, strict=True)
    ]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    float_path = args.output.with_suffix(".float.onnx")
    metadata = {
        "aura.corpus": "hermitdave/FrequencyWords en_50k derivative",
        "aura.corpus_source": "https://github.com/hermitdave/FrequencyWords",
        "aura.corpus_license": "MIT",
        "aura.corpus_license_sha256": license_sha,
        "aura.corpus_sha256": source_sha,
        "aura.builder_script": "android/tools/keyboard_model/train_reranker.py",
        "aura.builder_script_sha256": builder_sha,
        "aura.builder_numpy": np.__version__,
        "aura.builder_onnx": onnx.__version__,
        "aura.builder_onnxruntime": onnxruntime.__version__,
        "aura.feature_schema": FEATURE_SCHEMA,
        "aura.max_candidates": str(MAX_CANDIDATES),
        "aura.max_log_frequency": f"{math.log1p(max(frequency for _, frequency in entries)):.9f}",
        "aura.parameter_count": str(PARAMETER_COUNT),
        "aura.seed": str(SEED),
        "aura.training_examples": str(len(train_inputs)),
        "aura.validation_examples": str(len(validation_inputs)),
        "aura.validation_top1": f"{top1:.6f}",
        "aura.validation_top3": f"{top3:.6f}",
    }
    export_float(float_path, weights, metadata)
    quantize_dynamic(float_path, args.output, weight_type=QuantType.QInt8)
    float_path.unlink()
    model = onnx.load(args.output)
    del model.metadata_props[:]
    for key, value in sorted({**metadata, "aura.quantization": "dynamic int8 weights"}.items()):
        entry = model.metadata_props.add()
        entry.key = key
        entry.value = value
    onnx.checker.check_model(model)
    onnx.save_model(model, args.output)
    model_sha = hashlib.sha256(args.output.read_bytes()).hexdigest()
    report = {
        **metadata,
        "aura.quantization": "dynamic int8 weights",
        "model_bytes": args.output.stat().st_size,
        "model_sha256": model_sha,
    }
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
