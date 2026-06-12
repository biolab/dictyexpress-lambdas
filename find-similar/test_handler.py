from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("MATRIX_BUCKET", "test-bucket")

import handler  # noqa: E402
from common.cache import LruCache  # noqa: E402


def matrix_bytes(expression_ids: list[int] | None = None) -> bytes:
    buffer = io.BytesIO()
    matrix = {
        "genes": np.array(["gene-a", "gene-b"]),
        "expression": np.array([[1.0, 2.0], [2.0, 4.0]]),
        "rank_expression": np.array([[1.0, 2.0], [1.0, 2.0]]),
        "timepoints": np.array(["t1", "t2"]),
    }
    if expression_ids is not None:
        matrix["expression_ids"] = np.array(expression_ids, dtype=np.int64)
    np.savez_compressed(buffer, **matrix)
    return buffer.getvalue()


class FakeS3:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.puts: list[tuple[str, bytes]] = []

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes) -> None:
        self.objects[Key] = Body
        self.puts.append((Key, Body))


@pytest.fixture(autouse=True)
def reset_handler_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_BUCKET", "test-bucket")
    monkeypatch.setenv("MATRIX_PREFIX", "matrices")
    handler.MATRIX_CACHE = LruCache(1024 * 1024)


def test_expression_ids_are_part_of_exact_matrix_key() -> None:
    first = handler._matrix_key(42, (1, 2, 3))
    same_different_order = handler._matrix_key(
        42, handler._normalized_expression_ids([3, 2, 1])
    )
    second = handler._matrix_key(42, (1, 2, 4))

    assert first == same_different_order
    assert first != second
    assert handler._matrix_key(42) == "matrices/42.npz"


def test_load_matrix_reuses_legacy_matrix_only_when_expression_ids_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_key = handler._matrix_key(42)
    fake_s3 = FakeS3({legacy_key: matrix_bytes([3, 1, 2])})
    monkeypatch.setattr(handler, "S3", fake_s3)

    def fail_build(time_series_id: int, expression_ids: list[int]) -> dict[str, np.ndarray]:
        raise AssertionError("matching legacy matrix should not be rebuilt")

    monkeypatch.setattr(handler, "build_matrix_from_resolwe", fail_build)

    matrix = handler.load_matrix(42, [1, 2, 3])

    assert matrix["expression_ids"].tolist() == [3, 1, 2]
    assert fake_s3.puts == []


def test_load_matrix_builds_exact_matrix_when_legacy_metadata_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_key = handler._matrix_key(42)
    exact_key = handler._matrix_key(42, (1, 2, 3))
    fake_s3 = FakeS3({legacy_key: matrix_bytes()})
    monkeypatch.setattr(handler, "S3", fake_s3)

    def build_matrix(time_series_id: int, expression_ids: list[int]) -> dict[str, np.ndarray]:
        assert time_series_id == 42
        assert expression_ids == [1, 2, 3]
        loaded = np.load(io.BytesIO(matrix_bytes([1, 2, 3])), allow_pickle=False)
        return {key: loaded[key] for key in loaded.files}

    monkeypatch.setattr(handler, "build_matrix_from_resolwe", build_matrix)

    matrix = handler.load_matrix(42, [1, 2, 3])

    assert matrix["expression_ids"].tolist() == [1, 2, 3]
    assert fake_s3.puts[0][0] == exact_key

