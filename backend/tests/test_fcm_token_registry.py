"""
Tests for src/services/fcm_token_registry.py

Covers: register_token, get_user_tokens, remove_invalid_tokens
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


def _make_db(doc_exists: bool = True, doc_data: dict | None = None):
    """Build a minimal Firestore client mock."""
    doc_ref = MagicMock()
    doc_snap = MagicMock()
    doc_snap.exists = doc_exists
    doc_snap.to_dict = MagicMock(return_value=doc_data or {})
    doc_ref.get = MagicMock(return_value=doc_snap)

    col_ref = MagicMock()
    col_ref.document = MagicMock(return_value=doc_ref)

    user_doc_ref = MagicMock()
    user_doc_ref.collection = MagicMock(return_value=col_ref)

    users_col = MagicMock()
    users_col.document = MagicMock(return_value=user_doc_ref)

    db = MagicMock()
    db.collection = MagicMock(return_value=users_col)

    return db, doc_ref, col_ref


class TestRegisterToken:
    def test_new_token_calls_set(self):
        from src.services.fcm_token_registry import register_token

        db, doc_ref, _ = _make_db(doc_exists=False)
        doc_ref.get.return_value.exists = False

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            register_token("user1", "token_abc", "android")

        doc_ref.set.assert_called_once()
        args = doc_ref.set.call_args[0][0]
        assert args["token"] == "token_abc"
        assert args["platform"] == "android"
        assert "registered_at" in args

    def test_existing_token_calls_update(self):
        from src.services.fcm_token_registry import register_token

        db, doc_ref, _ = _make_db(doc_exists=True)
        doc_ref.get.return_value.exists = True

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            register_token("user1", "token_abc", "ios")

        doc_ref.update.assert_called_once()
        payload = doc_ref.update.call_args[0][0]
        assert payload["platform"] == "ios"
        assert "registered_at" in payload


class TestGetUserTokens:
    def test_returns_token_docs(self):
        from src.services.fcm_token_registry import get_user_tokens

        doc1 = MagicMock()
        doc1.exists = True
        doc1.to_dict = MagicMock(return_value={"token": "t1", "platform": "android"})
        doc2 = MagicMock()
        doc2.exists = True
        doc2.to_dict = MagicMock(return_value={"token": "t2", "platform": "ios"})

        col_ref = MagicMock()
        col_ref.stream = MagicMock(return_value=[doc1, doc2])
        user_doc = MagicMock()
        user_doc.collection = MagicMock(return_value=col_ref)
        users = MagicMock()
        users.document = MagicMock(return_value=user_doc)
        db = MagicMock()
        db.collection = MagicMock(return_value=users)

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            result = get_user_tokens("user1")

        assert len(result) == 2
        assert result[0]["token"] == "t1"

    def test_filters_out_none_dicts(self):
        from src.services.fcm_token_registry import get_user_tokens

        doc_none = MagicMock()
        doc_none.exists = True
        doc_none.to_dict = MagicMock(return_value=None)
        doc_ok = MagicMock()
        doc_ok.exists = True
        doc_ok.to_dict = MagicMock(return_value={"token": "t1"})

        col_ref = MagicMock()
        col_ref.stream = MagicMock(return_value=[doc_none, doc_ok])
        user_doc = MagicMock()
        user_doc.collection = MagicMock(return_value=col_ref)
        users = MagicMock()
        users.document = MagicMock(return_value=user_doc)
        db = MagicMock()
        db.collection = MagicMock(return_value=users)

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            result = get_user_tokens("user1")

        assert len(result) == 1

    def test_empty_returns_empty_list(self):
        from src.services.fcm_token_registry import get_user_tokens

        col_ref = MagicMock()
        col_ref.stream = MagicMock(return_value=[])
        user_doc = MagicMock()
        user_doc.collection = MagicMock(return_value=col_ref)
        users = MagicMock()
        users.document = MagicMock(return_value=user_doc)
        db = MagicMock()
        db.collection = MagicMock(return_value=users)

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            result = get_user_tokens("user1")

        assert result == []


class TestRemoveInvalidTokens:
    def test_deletes_each_token(self):
        from src.services.fcm_token_registry import remove_invalid_tokens

        doc_ref_a = MagicMock()
        doc_ref_b = MagicMock()
        col_ref = MagicMock()
        col_ref.document = MagicMock(side_effect=[doc_ref_a, doc_ref_b])
        user_doc = MagicMock()
        user_doc.collection = MagicMock(return_value=col_ref)
        users = MagicMock()
        users.document = MagicMock(return_value=user_doc)
        db = MagicMock()
        db.collection = MagicMock(return_value=users)

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            remove_invalid_tokens("user1", ["tok_a", "tok_b"])

        doc_ref_a.delete.assert_called_once()
        doc_ref_b.delete.assert_called_once()

    def test_empty_list_is_noop(self):
        from src.services.fcm_token_registry import remove_invalid_tokens

        db = MagicMock()

        with patch("src.services.fcm_token_registry.admin_firestore", return_value=db):
            remove_invalid_tokens("user1", [])

        db.collection.assert_not_called()
