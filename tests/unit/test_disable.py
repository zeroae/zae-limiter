"""Tests for resource/entity disable (ADR-125)."""

from zae_limiter import schema


class TestDisabledCodec:
    def test_encode_none_returns_none(self):
        assert schema.encode_disabled(None) is None

    def test_encode_true(self):
        assert schema.encode_disabled(True) == {"BOOL": True}

    def test_encode_false(self):
        assert schema.encode_disabled(False) == {"BOOL": False}

    def test_decode_absent_attribute_is_none(self):
        assert schema.decode_disabled({"PK": {"S": "ns/RESOURCE#gpt-4"}}) is None

    def test_decode_true(self):
        assert schema.decode_disabled({"disabled": {"BOOL": True}}) is True

    def test_decode_false_is_false_not_none(self):
        # Explicit False must be distinguishable from "inherit"
        assert schema.decode_disabled({"disabled": {"BOOL": False}}) is False
