"""Tests for attachment validation and content building in the chat handler."""

from __future__ import annotations

import base64

from src.handlers.chat import (
    _validate_and_filter_attachments,
    _build_user_content,
)


# ── _validate_and_filter_attachments ─────────────────────────────────────


class TestValidateAndFilterAttachments:
    def test_empty_list_returns_empty(self):
        accepted, rejected = _validate_and_filter_attachments([], "u1")
        assert accepted == []
        assert rejected == []

    def test_none_returns_empty(self):
        accepted, rejected = _validate_and_filter_attachments(None, "u1")  # type: ignore[arg-type]
        assert accepted == []
        assert rejected == []

    def test_valid_image_accepted(self):
        data = base64.b64encode(b"\xff\xd8\xff\xe0").decode()
        attachments = [{"type": "image", "mime_type": "image/jpeg", "data": data, "file_name": "photo.jpg"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 1
        assert len(rejected) == 0

    def test_valid_document_accepted(self):
        data = base64.b64encode(b"%PDF-1.4").decode()
        attachments = [{"type": "document", "mime_type": "application/pdf", "data": data, "file_name": "doc.pdf"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 1
        assert len(rejected) == 0

    def test_unsupported_mime_type_rejected(self):
        data = base64.b64encode(b"<svg></svg>").decode()
        attachments = [{"type": "image", "mime_type": "image/svg+xml", "data": data, "file_name": "icon.svg"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 0
        assert len(rejected) == 1
        assert "unsupported" in rejected[0].reason

    def test_missing_data_rejected(self):
        attachments = [{"type": "image", "mime_type": "image/jpeg", "data": "", "file_name": "empty.jpg"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 0
        assert len(rejected) == 1

    def test_exceeding_max_count_rejected(self):
        data = base64.b64encode(b"x").decode()
        attachments = [
            {"type": "image", "mime_type": "image/jpeg", "data": data, "file_name": f"img{i}.jpg"}
            for i in range(7)
        ]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 5
        assert len(rejected) == 2

    def test_oversized_image_rejected(self):
        data = "x" * 8_000_000  # > 7 MB base64 limit
        attachments = [{"type": "image", "mime_type": "image/jpeg", "data": data, "file_name": "big.jpg"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 0
        assert len(rejected) == 1
        assert "5 MB" in rejected[0].reason

    def test_oversized_document_rejected(self):
        data = "x" * 15_000_000  # > 14 MB base64 limit
        attachments = [{"type": "document", "mime_type": "application/pdf", "data": data, "file_name": "huge.pdf"}]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 0
        assert len(rejected) == 1
        assert "10 MB" in rejected[0].reason

    def test_mixed_valid_and_invalid(self):
        good_data = base64.b64encode(b"ok").decode()
        attachments = [
            {"type": "image", "mime_type": "image/jpeg", "data": good_data, "file_name": "good.jpg"},
            {"type": "image", "mime_type": "image/svg+xml", "data": good_data, "file_name": "bad.svg"},
        ]
        accepted, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert len(accepted) == 1
        assert len(rejected) == 1

    def test_non_dict_items_skipped(self):
        accepted, rejected = _validate_and_filter_attachments(["not a dict", 42], "u1")
        assert accepted == []
        assert rejected == []

    def test_rejection_includes_file_name(self):
        attachments = [{"type": "image", "mime_type": "image/bmp", "data": "x", "file_name": "photo.bmp"}]
        _, rejected = _validate_and_filter_attachments(attachments, "u1")
        assert rejected[0].file_name == "photo.bmp"


# ── _build_user_content ──────────────────────────────────────────────────


class TestBuildUserContent:
    def test_no_attachments_returns_plain_string(self):
        result = _build_user_content("Hello", [])
        assert result == "Hello"

    def test_image_attachment_builds_image_block(self):
        data = base64.b64encode(b"\xff\xd8").decode()
        att = {"type": "image", "mime_type": "image/jpeg", "data": data}
        result = _build_user_content("Describe this", [att])
        assert isinstance(result, list)
        assert result[0]["type"] == "image"
        assert result[0]["source"]["type"] == "base64"
        assert result[0]["source"]["media_type"] == "image/jpeg"
        assert result[1] == {"type": "text", "text": "Describe this"}

    def test_document_attachment_builds_document_block(self):
        data = base64.b64encode(b"%PDF").decode()
        att = {"type": "document", "mime_type": "application/pdf", "data": data}
        result = _build_user_content("Summarize", [att])
        assert isinstance(result, list)
        assert result[0]["type"] == "document"
        assert result[0]["source"]["media_type"] == "application/pdf"

    def test_mixed_attachments(self):
        img_data = base64.b64encode(b"\xff\xd8").decode()
        doc_data = base64.b64encode(b"%PDF").decode()
        attachments = [
            {"type": "image", "mime_type": "image/jpeg", "data": img_data},
            {"type": "document", "mime_type": "application/pdf", "data": doc_data},
        ]
        result = _build_user_content("Look at both", attachments)
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["type"] == "image"
        assert result[1]["type"] == "document"
        assert result[2] == {"type": "text", "text": "Look at both"}

    def test_empty_message_with_attachments_omits_text_block(self):
        data = base64.b64encode(b"\xff\xd8").decode()
        att = {"type": "image", "mime_type": "image/jpeg", "data": data}
        result = _build_user_content("", [att])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "image"
