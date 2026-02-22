"""Tests for DynamoDB schema key builders with namespace support (Issue #364)."""

import pytest

from zae_limiter import schema
from zae_limiter.schema import (
    # New constants
    DEFAULT_NAMESPACE,
    GSI4_NAME,
    RESERVED_NAMESPACE,
    SK_NAMESPACE_PREFIX,
    SK_NSID_PREFIX,
    # Existing utility functions (unchanged)
    bucket_attr,
    # Table definition
    get_table_definition,
    # Existing PK/GSI-PK builders (updated signatures)
    gsi1_pk_parent,
    # Existing GSI SK builders (unchanged)
    gsi1_sk_child,
    gsi2_pk_resource,
    gsi2_sk_access,
    gsi2_sk_bucket,
    gsi2_sk_usage,
    gsi3_pk_entity_config,
    gsi3_sk_entity,
    limit_attr,
    parse_bucket_attr,
    parse_bucket_sk,
    parse_limit_attr,
    # New functions
    parse_namespace,
    pk_audit,
    pk_entity,
    pk_resource,
    pk_system,
    sk_audit,
    sk_bucket,
    sk_config,
    sk_entity_config_resources,
    sk_limit,
    sk_limit_prefix,
    # Existing SK builders (unchanged signatures)
    sk_meta,
    sk_namespace,
    sk_namespace_prefix,
    sk_nsid,
    sk_nsid_prefix,
    sk_resource,
    sk_resource_limit,
    sk_resource_limit_prefix,
    sk_resources,
    sk_system_limit,
    sk_system_limit_prefix,
    sk_usage,
    sk_version,
)

# =============================================================================
# Step 1: Constants
# =============================================================================


class TestConstants:
    """Test namespace constants."""

    def test_reserved_namespace(self):
        assert RESERVED_NAMESPACE == "_"

    def test_default_namespace(self):
        assert DEFAULT_NAMESPACE == "default"

    def test_gsi4_name(self):
        assert GSI4_NAME == "GSI4"

    def test_sk_namespace_prefix_constant(self):
        assert SK_NAMESPACE_PREFIX == "#NAMESPACE#"

    def test_sk_nsid_prefix_constant(self):
        assert SK_NSID_PREFIX == "#NSID#"


# =============================================================================
# Step 2: PK/GSI-PK builders with namespace_id
# =============================================================================


class TestPKBuildersWithNamespace:
    """Test partition key builders accept namespace_id as first positional param."""

    @pytest.mark.parametrize(
        "ns_id, entity_id, expected",
        [
            ("a7x3kq", "user-123", "a7x3kq/ENTITY#user-123"),
            ("_", "user-123", "_/ENTITY#user-123"),
            ("default", "org-abc", "default/ENTITY#org-abc"),
        ],
    )
    def test_pk_entity(self, ns_id, entity_id, expected):
        assert pk_entity(ns_id, entity_id) == expected

    @pytest.mark.parametrize(
        "ns_id, expected",
        [
            ("a7x3kq", "a7x3kq/SYSTEM#"),
            ("_", "_/SYSTEM#"),
            ("default", "default/SYSTEM#"),
        ],
    )
    def test_pk_system(self, ns_id, expected):
        assert pk_system(ns_id) == expected

    @pytest.mark.parametrize(
        "ns_id, resource, expected",
        [
            ("a7x3kq", "gpt-4", "a7x3kq/RESOURCE#gpt-4"),
            ("_", "claude-3", "_/RESOURCE#claude-3"),
        ],
    )
    def test_pk_resource(self, ns_id, resource, expected):
        assert pk_resource(ns_id, resource) == expected

    @pytest.mark.parametrize(
        "ns_id, entity_id, expected",
        [
            ("a7x3kq", "user-123", "a7x3kq/AUDIT#user-123"),
            ("_", "$SYSTEM", "_/AUDIT#$SYSTEM"),
        ],
    )
    def test_pk_audit(self, ns_id, entity_id, expected):
        assert pk_audit(ns_id, entity_id) == expected

    @pytest.mark.parametrize(
        "ns_id, parent_id, expected",
        [
            ("a7x3kq", "org-abc", "a7x3kq/PARENT#org-abc"),
            ("_", "parent-1", "_/PARENT#parent-1"),
        ],
    )
    def test_gsi1_pk_parent(self, ns_id, parent_id, expected):
        assert gsi1_pk_parent(ns_id, parent_id) == expected

    @pytest.mark.parametrize(
        "ns_id, resource, expected",
        [
            ("a7x3kq", "gpt-4", "a7x3kq/RESOURCE#gpt-4"),
            ("_", "claude-3", "_/RESOURCE#claude-3"),
        ],
    )
    def test_gsi2_pk_resource(self, ns_id, resource, expected):
        assert gsi2_pk_resource(ns_id, resource) == expected

    @pytest.mark.parametrize(
        "ns_id, resource, expected",
        [
            ("a7x3kq", "gpt-4", "a7x3kq/ENTITY_CONFIG#gpt-4"),
            ("_", "claude-3", "_/ENTITY_CONFIG#claude-3"),
        ],
    )
    def test_gsi3_pk_entity_config(self, ns_id, resource, expected):
        assert gsi3_pk_entity_config(ns_id, resource) == expected


# =============================================================================
# Step 3: parse_namespace()
# =============================================================================


class TestParseNamespace:
    """Test parse_namespace() extracts namespace from prefixed keys."""

    def test_entity_key(self):
        assert parse_namespace("a7x3kq/ENTITY#user-123") == ("a7x3kq", "ENTITY#user-123")

    def test_system_key(self):
        assert parse_namespace("_/SYSTEM#") == ("_", "SYSTEM#")

    def test_splits_on_first_slash(self):
        assert parse_namespace("ns/with/slashes/ENTITY#x") == ("ns", "with/slashes/ENTITY#x")

    def test_no_slash_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_namespace("noslash")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_namespace("")

    def test_resource_key(self):
        assert parse_namespace("default/RESOURCE#gpt-4") == ("default", "RESOURCE#gpt-4")

    def test_audit_key(self):
        assert parse_namespace("a7x3kq/AUDIT#user-123") == ("a7x3kq", "AUDIT#user-123")


# =============================================================================
# Step 4: Namespace registry sort key builders
# =============================================================================


class TestNamespaceRegistrySKBuilders:
    """Test namespace registry sort key builders."""

    def test_sk_namespace(self):
        assert sk_namespace("default") == "#NAMESPACE#default"

    def test_sk_namespace_custom(self):
        assert sk_namespace("my-namespace") == "#NAMESPACE#my-namespace"

    def test_sk_nsid(self):
        assert sk_nsid("a7x3kq") == "#NSID#a7x3kq"

    def test_sk_namespace_prefix_func(self):
        assert sk_namespace_prefix() == "#NAMESPACE#"

    def test_sk_nsid_prefix_func(self):
        assert sk_nsid_prefix() == "#NSID#"


# =============================================================================
# Step 5: Existing SK builders unchanged (no namespace_id param)
# =============================================================================


class TestExistingSKBuildersUnchanged:
    """Verify existing sort key builders have NOT changed signatures."""

    def test_sk_meta(self):
        assert sk_meta() == "#META"

    def test_sk_version(self):
        assert sk_version() == "#VERSION"

    def test_sk_bucket(self):
        assert sk_bucket("gpt-4") == "#BUCKET#gpt-4"

    def test_sk_config_system(self):
        assert sk_config() == "#CONFIG"

    def test_sk_config_entity(self):
        assert sk_config("gpt-4") == "#CONFIG#gpt-4"

    def test_sk_resources(self):
        assert sk_resources() == "#RESOURCES"

    def test_sk_entity_config_resources(self):
        assert sk_entity_config_resources() == "#ENTITY_CONFIG_RESOURCES"

    def test_sk_audit(self):
        assert sk_audit("evt-1") == "#AUDIT#evt-1"

    def test_sk_usage(self):
        assert sk_usage("gpt-4", "2024-01-01T00") == "#USAGE#gpt-4#2024-01-01T00"

    def test_sk_resource(self):
        assert sk_resource("gpt-4") == "#RESOURCE#gpt-4"

    def test_sk_limit(self):
        assert sk_limit("gpt-4", "rpm") == "#LIMIT#gpt-4#rpm"

    def test_sk_limit_prefix(self):
        assert sk_limit_prefix("gpt-4") == "#LIMIT#gpt-4#"

    def test_sk_system_limit(self):
        assert sk_system_limit("rpm") == "#LIMIT#rpm"

    def test_sk_system_limit_prefix(self):
        assert sk_system_limit_prefix() == "#LIMIT#"

    def test_sk_resource_limit(self):
        assert sk_resource_limit("rpm") == "#LIMIT#rpm"

    def test_sk_resource_limit_prefix(self):
        assert sk_resource_limit_prefix() == "#LIMIT#"


class TestExistingUtilityFunctionsUnchanged:
    """Verify utility functions have NOT changed."""

    def test_bucket_attr(self):
        assert bucket_attr("rpm", "tk") == "b_rpm_tk"

    def test_parse_bucket_attr(self):
        assert parse_bucket_attr("b_rpm_tk") == ("rpm", "tk")

    def test_parse_bucket_attr_not_bucket(self):
        assert parse_bucket_attr("other") is None

    def test_limit_attr(self):
        assert limit_attr("rpm", "cp") == "l_rpm_cp"

    def test_parse_limit_attr(self):
        assert parse_limit_attr("l_rpm_cp") == ("rpm", "cp")

    def test_parse_limit_attr_not_limit(self):
        assert parse_limit_attr("other") is None

    def test_parse_bucket_sk(self):
        assert parse_bucket_sk("#BUCKET#gpt-4") == "gpt-4"


class TestExistingGSISKBuildersUnchanged:
    """Verify existing GSI sort key builders have NOT changed."""

    def test_gsi1_sk_child(self):
        assert gsi1_sk_child("child-1") == "CHILD#child-1"

    def test_gsi2_sk_bucket(self):
        assert gsi2_sk_bucket("user-1") == "BUCKET#user-1#0"

    def test_gsi2_sk_access(self):
        assert gsi2_sk_access("user-1") == "ACCESS#user-1"

    def test_gsi2_sk_usage(self):
        assert gsi2_sk_usage("2024-01", "user-1") == "USAGE#2024-01#user-1"

    def test_gsi3_sk_entity(self):
        assert gsi3_sk_entity("user-1") == "user-1"


# =============================================================================
# Step 6: get_table_definition() includes GSI4
# =============================================================================


class TestGetTableDefinitionGSI4:
    """Test that get_table_definition() includes GSI4."""

    def test_gsi4_in_attribute_definitions(self):
        defn = get_table_definition("test-table")
        attr_names = [a["AttributeName"] for a in defn["AttributeDefinitions"]]
        assert "GSI4PK" in attr_names
        assert "GSI4SK" in attr_names

    def test_gsi4_in_global_secondary_indexes(self):
        defn = get_table_definition("test-table")
        gsi_names = [g["IndexName"] for g in defn["GlobalSecondaryIndexes"]]
        assert "GSI4" in gsi_names

    def test_gsi4_keys_only_projection(self):
        defn = get_table_definition("test-table")
        gsi4 = next(g for g in defn["GlobalSecondaryIndexes"] if g["IndexName"] == "GSI4")
        assert gsi4["Projection"]["ProjectionType"] == "KEYS_ONLY"

    def test_gsi4_key_schema(self):
        defn = get_table_definition("test-table")
        gsi4 = next(g for g in defn["GlobalSecondaryIndexes"] if g["IndexName"] == "GSI4")
        key_schema = {k["AttributeName"]: k["KeyType"] for k in gsi4["KeySchema"]}
        assert key_schema == {"GSI4PK": "HASH", "GSI4SK": "RANGE"}

    def test_existing_gsis_still_present(self):
        defn = get_table_definition("test-table")
        gsi_names = [g["IndexName"] for g in defn["GlobalSecondaryIndexes"]]
        assert "GSI1" in gsi_names
        assert "GSI2" in gsi_names
        assert "GSI3" in gsi_names


# =============================================================================
# Provisioner state key builder (Issue #405)
# =============================================================================


class TestProvisionerKey:
    """Tests for provisioner state sort key builder."""

    def test_sk_provisioner(self):
        from zae_limiter.schema import sk_provisioner

        assert sk_provisioner() == "#PROVISIONER"


# =============================================================================
# Bucket PK builders (Pre-Shard Buckets, GHSA-76rv)
# =============================================================================


class TestBucketPKBuilders:
    """Tests for new bucket partition key builders."""

    def test_pk_bucket(self):
        assert schema.pk_bucket("ns1", "user-1", "gpt-4", 0) == "ns1/BUCKET#user-1#gpt-4#0"
        assert schema.pk_bucket("ns1", "user-1", "gpt-4", 3) == "ns1/BUCKET#user-1#gpt-4#3"

    def test_sk_state(self):
        assert schema.sk_state() == "#STATE"

    def test_parse_bucket_pk(self):
        ns, entity, resource, shard = schema.parse_bucket_pk("ns1/BUCKET#user-1#gpt-4#0")
        assert ns == "ns1"
        assert entity == "user-1"
        assert resource == "gpt-4"
        assert shard == 0

    def test_parse_bucket_pk_multi_shard(self):
        ns, entity, resource, shard = schema.parse_bucket_pk("ns1/BUCKET#user-1#gpt-4#3")
        assert shard == 3

    def test_parse_bucket_pk_invalid(self):
        with pytest.raises(ValueError):
            schema.parse_bucket_pk("ns1/ENTITY#user-1")

    def test_parse_bucket_pk_missing_parts(self):
        with pytest.raises(ValueError):
            schema.parse_bucket_pk("ns1/BUCKET#onlyonepart")

    def test_parse_bucket_pk_missing_resource_separator(self):
        with pytest.raises(ValueError):
            schema.parse_bucket_pk("ns1/BUCKET#entityonly#0")

    def test_gsi3_pk_entity(self):
        assert schema.gsi3_pk_entity("ns1", "user-1") == "ns1/ENTITY#user-1"

    def test_gsi3_sk_bucket(self):
        assert schema.gsi3_sk_bucket("gpt-4", 0) == "BUCKET#gpt-4#0"
        assert schema.gsi3_sk_bucket("gpt-4", 3) == "BUCKET#gpt-4#3"

    @pytest.mark.parametrize(
        "resource",
        [
            "gpt-4",  # hyphen
            "gpt_4",  # underscore
            "gpt-3.5-turbo",  # dot
            "openai/gpt-4",  # slash (provider/model)
            "anthropic/claude-3/opus",  # nested slash
        ],
    )
    def test_parse_bucket_pk_round_trip(self, resource):
        """pk_bucket -> parse_bucket_pk round-trips for all valid resource chars."""
        pk = schema.pk_bucket("ns1", "user-1", resource, 0)
        ns, entity, res, shard = schema.parse_bucket_pk(pk)
        assert ns == "ns1"
        assert entity == "user-1"
        assert res == resource
        assert shard == 0

    def test_gsi4_sk_bucket(self):
        assert schema.gsi4_sk_bucket("user-1", "gpt-4", 0) == "BUCKET#user-1#gpt-4#0"
        assert schema.gsi4_sk_bucket("user-1", "gpt-4", 3) == "BUCKET#user-1#gpt-4#3"

    def test_gsi2_sk_bucket_with_shard(self):
        assert gsi2_sk_bucket("user-1", 0) == "BUCKET#user-1#0"
        assert gsi2_sk_bucket("user-1", 3) == "BUCKET#user-1#3"


class TestWCULimitConstants:
    """Tests for WCU infrastructure limit constants."""

    def test_wcu_limit_constants(self):
        assert schema.WCU_LIMIT_NAME == "wcu"
        assert schema.WCU_LIMIT_CAPACITY == 1000
        assert schema.WCU_LIMIT_REFILL_AMOUNT == 1000
        assert schema.WCU_LIMIT_REFILL_PERIOD_SECONDS == 1
