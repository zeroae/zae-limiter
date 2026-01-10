"""Tests for models."""

import pytest

from zae_limiter import Entity, Limit, LimitName


class TestLimit:
    """Tests for Limit model."""

    def test_per_minute(self):
        """Test per_minute factory."""
        limit = Limit.per_minute("rpm", 100)
        assert limit.name == "rpm"
        assert limit.capacity == 100
        assert limit.burst == 100
        assert limit.refill_amount == 100
        assert limit.refill_period_seconds == 60

    def test_per_minute_with_burst(self):
        """Test per_minute with custom burst."""
        limit = Limit.per_minute("rpm", 100, burst=150)
        assert limit.capacity == 100
        assert limit.burst == 150

    def test_per_hour(self):
        """Test per_hour factory."""
        limit = Limit.per_hour("rph", 1000)
        assert limit.refill_period_seconds == 3600

    def test_per_day(self):
        """Test per_day factory."""
        limit = Limit.per_day("rpd", 10000)
        assert limit.refill_period_seconds == 86400

    def test_per_second(self):
        """Test per_second factory."""
        limit = Limit.per_second("rps", 10)
        assert limit.refill_period_seconds == 1

    def test_custom(self):
        """Test custom limit configuration."""
        limit = Limit.custom(
            name="custom",
            capacity=100,
            refill_amount=50,
            refill_period_seconds=30,
            burst=200,
        )
        assert limit.capacity == 100
        assert limit.burst == 200
        assert limit.refill_amount == 50
        assert limit.refill_period_seconds == 30

    def test_refill_rate_property(self):
        """Test refill_rate calculation."""
        limit = Limit.per_minute("rpm", 60)
        assert limit.refill_rate == 1.0  # 1 token per second

    def test_invalid_capacity(self):
        """Test validation of capacity."""
        with pytest.raises(ValueError, match="capacity must be positive"):
            Limit.per_minute("rpm", 0)

    def test_invalid_burst(self):
        """Test validation of burst < capacity."""
        with pytest.raises(ValueError, match="burst must be >= capacity"):
            Limit.per_minute("rpm", 100, burst=50)

    def test_to_dict_from_dict(self):
        """Test serialization round-trip."""
        limit = Limit.per_minute("rpm", 100, burst=150)
        data = limit.to_dict()
        restored = Limit.from_dict(data)
        assert restored == limit

    def test_frozen(self):
        """Test that Limit is immutable."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(AttributeError):
            limit.capacity = 200


class TestEntity:
    """Tests for Entity model."""

    def test_parent_entity(self):
        """Test parent entity (no parent_id)."""
        entity = Entity(id="proj-1", name="Project 1")
        assert entity.is_parent is True
        assert entity.is_child is False

    def test_child_entity(self):
        """Test child entity (has parent_id)."""
        entity = Entity(id="key-1", name="Key 1", parent_id="proj-1")
        assert entity.is_parent is False
        assert entity.is_child is True

    def test_default_metadata(self):
        """Test default metadata is empty dict."""
        entity = Entity(id="test")
        assert entity.metadata == {}


class TestLimitName:
    """Tests for LimitName constants."""

    def test_constants(self):
        """Test limit name constants."""
        assert LimitName.RPM == "rpm"
        assert LimitName.TPM == "tpm"
        assert LimitName.RPH == "rph"
        assert LimitName.TPH == "tph"
