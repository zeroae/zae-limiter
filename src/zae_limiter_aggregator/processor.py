"""DynamoDB Stream processor for usage aggregation and bucket refill."""

import json
import time as time_module
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from zae_limiter.bucket import refill_bucket
from zae_limiter.schedule import (
    ScheduleEntry,
    decode,
    effective_params,
    next_boundary,
)
from zae_limiter.schema import (
    BUCKET_ATTR_PREFIX,
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_SCHED_TZ,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_PREFIX,
    SK_BUCKET,
    WCU_LIMIT_NAME,
    WCU_SHARD_WARN_THRESHOLD,
    bucket_attr,
    gsi2_pk_resource,
    gsi2_sk_bucket,
    gsi2_sk_usage,
    gsi3_sk_bucket,
    gsi4_sk_bucket,
    parse_bucket_pk,
    parse_namespace,
    pk_bucket,
    pk_entity,
    sk_state,
    sk_usage,
)


class StructuredLogger:
    """JSON-formatted logger for CloudWatch Logs Insights."""

    def __init__(self, name: str):
        self._name = name

    def _log(self, level: str, message: str, **extra: Any) -> None:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "logger": self._name,
            "message": message,
            **extra,
        }
        print(json.dumps(log_entry))

    def debug(self, message: str, **extra: Any) -> None:
        self._log("DEBUG", message, **extra)

    def info(self, message: str, **extra: Any) -> None:
        self._log("INFO", message, **extra)

    def warning(self, message: str, exc_info: bool = False, **extra: Any) -> None:
        if exc_info:
            extra["exception"] = traceback.format_exc()
        self._log("WARNING", message, **extra)

    def error(self, message: str, exc_info: bool = False, **extra: Any) -> None:
        if exc_info:
            extra["exception"] = traceback.format_exc()
        self._log("ERROR", message, **extra)


logger = StructuredLogger(__name__)


@dataclass
class ProcessResult:
    """Result of processing stream records."""

    processed_count: int
    snapshots_updated: int
    refills_written: int
    errors: list[str]


@dataclass
class ConsumptionDelta:
    """Consumption delta extracted from stream record."""

    namespace_id: str
    entity_id: str
    resource: str
    limit_name: str
    tokens_delta: int  # positive = consumed, negative = refilled/returned
    timestamp_ms: int


@dataclass
class LimitRefillInfo:
    """Per-limit bucket fields needed for refill calculation."""

    tc_delta: int  # accumulated tc delta (millitokens)
    tk_milli: int  # last NewImage tokens (millitokens)
    cp_milli: int  # capacity / ceiling (millitokens)
    ra_milli: int  # refill_amount (millitokens)
    rp_ms: int  # refill_period (milliseconds)
    # Schedule in force for *this* limit (#222): the item-level default unless
    # the item carries a `b_{name}_sched` override. Empty means unscheduled.
    sched: tuple[ScheduleEntry, ...] = ()


@dataclass
class BucketRefillState:
    """Per-bucket aggregated state for refill decision.

    Groups all limits for a composite bucket (entity+resource+shard) together
    with the shared ``rf`` timestamp.
    """

    namespace_id: str
    entity_id: str
    resource: str
    rf_ms: int  # shared refill timestamp (optimistic lock)
    limits: dict[str, LimitRefillInfo] = field(default_factory=dict)
    shard_id: int = 0
    shard_count: int = 1
    # Item-level default schedule (#222), decoded, plus the raw compact string
    # the decode came from. The raw form is what the write conditions on, so a
    # refill computed from a pre-fan-out image cannot land on an item whose
    # schedule has since changed.
    sched: tuple[ScheduleEntry, ...] = ()
    sched_compact: str | None = None
    # Item-level materialisation stamp. None when the attribute is absent.
    vu_ms: int | None = None
    # Set when a stored schedule could not be decoded (§6 — realistically a
    # newer client's encoding). The bucket is then skipped entirely rather
    # than refilled at the *base* rate, which would silently undo a scale-down.
    sched_error: str | None = None


def process_stream_records(
    records: list[dict[str, Any]],
    table_name: str,
    windows: list[str],
    ttl_days: int = 90,
) -> ProcessResult:
    """
    Process DynamoDB stream records and update usage snapshots.

    1. Filter for BUCKET records (MODIFY events)
    2. Extract consumption deltas from old/new images
    3. Aggregate into hourly/daily snapshot records
    4. Write updates using atomic ADD operations

    Args:
        records: DynamoDB stream records
        table_name: Target table name
        windows: List of window types ("hourly", "daily")
        ttl_days: TTL for snapshot records

    Returns:
        ProcessResult with counts and errors
    """
    start_time = time_module.perf_counter()

    logger.info(
        "Batch processing started",
        record_count=len(records),
        windows=windows,
        table_name=table_name,
    )

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    deltas: list[ConsumptionDelta] = []
    errors: list[str] = []

    # Extract deltas from records
    for idx, record in enumerate(records):
        if record.get("eventName") != "MODIFY":
            continue

        try:
            record_deltas = extract_deltas(record)
            deltas.extend(record_deltas)
        except Exception as e:
            error_msg = f"Error processing record: {e}"
            logger.warning(
                error_msg,
                exc_info=True,
                record_index=idx,
            )
            errors.append(error_msg)

    if not deltas:
        processing_time_ms = (time_module.perf_counter() - start_time) * 1000
        logger.info(
            "Batch processing completed",
            processed_count=len(records),
            deltas_extracted=0,
            snapshots_updated=0,
            refills_written=0,
            error_count=len(errors),
            processing_time_ms=round(processing_time_ms, 2),
        )
        return ProcessResult(len(records), 0, 0, errors)

    # Update snapshots
    snapshots_updated = 0
    for delta in deltas:
        for window in windows:
            try:
                update_snapshot(table, delta, window, ttl_days)
                snapshots_updated += 1
            except Exception as e:
                error_msg = f"Error updating snapshot: {e}"
                logger.warning(
                    error_msg,
                    exc_info=True,
                    entity_id=delta.entity_id,
                    resource=delta.resource,
                    limit_name=delta.limit_name,
                    window=window,
                )
                errors.append(error_msg)

    # Refill buckets proactively (Issue #317)
    refills_written = 0
    bucket_states = aggregate_bucket_states(records)
    now_ms = int(time_module.time() * 1000)

    for state in bucket_states.values():
        try:
            if try_refill_bucket(table, state, now_ms):
                refills_written += 1
        except Exception as e:
            error_msg = f"Error refilling bucket: {e}"
            logger.warning(
                error_msg,
                exc_info=True,
                entity_id=state.entity_id,
                resource=state.resource,
            )
            errors.append(error_msg)

    # Proactive sharding (check wcu token level per bucket)
    for state in bucket_states.values():
        wcu_info = state.limits.get(WCU_LIMIT_NAME)
        if wcu_info:
            try:
                try_proactive_shard(
                    table,
                    state,
                    wcu_tk_milli=wcu_info.tk_milli,
                    wcu_capacity_milli=wcu_info.cp_milli,
                )
            except Exception as e:
                error_msg = f"Error in proactive sharding: {e}"
                logger.warning(
                    error_msg,
                    exc_info=True,
                    entity_id=state.entity_id,
                    resource=state.resource,
                )
                errors.append(error_msg)

    # Propagate shard_count changes to other shards
    for record in records:
        if record.get("eventName") != "MODIFY":
            continue
        try:
            propagate_shard_count(table, record, now_ms)
        except Exception as e:
            error_msg = f"Error propagating shard_count: {e}"
            logger.warning(
                error_msg,
                exc_info=True,
            )
            errors.append(error_msg)

    processing_time_ms = (time_module.perf_counter() - start_time) * 1000
    logger.info(
        "Batch processing completed",
        processed_count=len(records),
        deltas_extracted=len(deltas),
        snapshots_updated=snapshots_updated,
        refills_written=refills_written,
        error_count=len(errors),
        processing_time_ms=round(processing_time_ms, 2),
    )

    return ProcessResult(len(records), snapshots_updated, refills_written, errors)


@dataclass
class ParsedBucketLimit:
    """Parsed per-limit fields from a composite bucket stream record."""

    tc_delta: int  # new_tc - old_tc (millitokens)
    tk_milli: int  # tokens from NewImage
    cp_milli: int  # capacity / ceiling from NewImage
    ra_milli: int  # refill_amount from NewImage
    rp_ms: int  # refill_period from NewImage
    sched: tuple[ScheduleEntry, ...] = ()  # per-limit schedule (#222)


@dataclass
class ParsedBucketRecord:
    """Parsed composite bucket stream record."""

    namespace_id: str
    entity_id: str
    resource: str
    rf_ms: int  # shared refill timestamp from NewImage
    limits: dict[str, ParsedBucketLimit]
    shard_id: int = 0
    shard_count: int = 1
    sched: tuple[ScheduleEntry, ...] = ()  # item-level default schedule (#222)
    sched_compact: str | None = None  # raw stored form of ``sched``
    vu_ms: int | None = None  # materialisation stamp, None when absent
    sched_error: str | None = None  # set when a stored schedule would not decode


def _decode_schedule(compact: str | None, tz: str) -> tuple[tuple[ScheduleEntry, ...], str | None]:
    """Decode a stored compact schedule into ``(schedule, error)``.

    A schedule that will not decode is reported rather than raised: the caller
    degrades to "do not touch this bucket". Raising here would abort the whole
    stream batch — including the usage snapshots, which do not care about
    schedules — and a poison-pill record would then retry until the stream
    stalled. §6 puts the decision about an unreadable schedule on the client,
    where the operator's ``on_unavailable`` setting lives.
    """
    if not compact:
        return (), None
    try:
        return decode(compact, tz), None
    except ValueError as e:
        return (), f"{compact!r} ({tz}): {e}"


def _parse_bucket_record(record: dict[str, Any]) -> ParsedBucketRecord | None:
    """Parse a composite bucket stream record into structured fields.

    Supports both PK formats:
    - New: PK={ns}/BUCKET#{entity}#{resource}#{shard}, SK=#STATE
    - Old: PK={ns}/ENTITY#{entity}, SK=#BUCKET#{resource}

    Shared by :func:`extract_deltas` and :func:`aggregate_bucket_states` to
    avoid duplicating the DynamoDB stream image parsing logic.

    Args:
        record: DynamoDB stream record (must be a MODIFY event on a bucket SK)

    Returns:
        ParsedBucketRecord or None if the record is not a valid bucket MODIFY.
    """
    dynamodb_data = record.get("dynamodb", {})
    new_image = dynamodb_data.get("NewImage", {})
    old_image = dynamodb_data.get("OldImage", {})

    pk = new_image.get("PK", {}).get("S", "")
    sk = new_image.get("SK", {}).get("S", "")

    # Try new PK format: {ns}/BUCKET#{entity}#{resource}#{shard}, SK=#STATE
    try:
        namespace_id, remainder = parse_namespace(pk)
    except ValueError:
        logger.warning(
            "Skipping pre-migration record with unprefixed PK",
            pk=pk,
        )
        return None

    shard_id = 0
    shard_count = 1

    if remainder.startswith(BUCKET_PREFIX):
        # New PK format
        try:
            namespace_id, entity_id, resource, shard_id = parse_bucket_pk(pk)
        except ValueError:
            return None
        shard_count = int(new_image.get("shard_count", {}).get("N", "1"))
    elif sk.startswith(SK_BUCKET):
        # Old PK format: PK={ns}/ENTITY#{entity}, SK=#BUCKET#{resource}
        resource = sk[len(SK_BUCKET) :]
        if not resource:
            return None
        entity_id = new_image.get("entity_id", {}).get("S", "")
        if not entity_id:
            return None
        shard_count = int(new_image.get("shard_count", {}).get("N", "1"))
    else:
        return None

    entity_id_check = new_image.get("entity_id", {}).get("S", "")
    if not entity_id_check:
        return None

    rf_ms = int(new_image.get("rf", {}).get("N", "0"))

    # Scheduled limits (#222). The bucket item carries its own schedule, so the
    # aggregator evaluates one without ever reading config — the same property
    # the client fast path relies on.
    sched_tz = new_image.get(BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    sched_compact = new_image.get(BUCKET_FIELD_SCHED, {}).get("S")
    sched, decode_error = _decode_schedule(sched_compact, sched_tz)
    sched_error = f"item schedule {decode_error}" if decode_error else None

    vu_raw = new_image.get(BUCKET_FIELD_VU, {}).get("N")
    vu_ms = int(vu_raw) if vu_raw is not None else None

    # Discover limits by scanning b_{name}_tc attributes
    limits: dict[str, ParsedBucketLimit] = {}
    for attr_name in new_image:
        if not attr_name.startswith(BUCKET_ATTR_PREFIX):
            continue
        rest = attr_name[len(BUCKET_ATTR_PREFIX) :]
        idx = rest.rfind("_")
        if idx <= 0:
            continue
        if rest[idx + 1 :] != BUCKET_FIELD_TC:
            continue
        limit_name = rest[:idx]
        if not limit_name:
            continue

        tc_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_TC}"
        new_tc_raw = new_image.get(tc_attr, {}).get("N")
        old_tc_raw = old_image.get(tc_attr, {}).get("N")
        if new_tc_raw is None or old_tc_raw is None:
            logger.debug(
                "Skipping limit without consumption counter",
                entity_id=entity_id,
                resource=resource,
                limit_name=limit_name,
            )
            continue

        tc_delta = int(new_tc_raw) - int(old_tc_raw)

        tk_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_TK}"
        cp_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_CP}"
        ra_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_RA}"
        rp_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_RP}"

        # `b_{name}_sched` overrides the item-level default for this limit
        # only (§4.1). Ignoring it would refill an overridden limit at the
        # item default's rate — over-refilling whenever the override is the
        # tighter of the two.
        limit_sched = sched
        sched_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_SCHED}"
        limit_compact = new_image.get(sched_attr, {}).get("S")
        if limit_compact and limit_compact != sched_compact:
            limit_sched, decode_error = _decode_schedule(limit_compact, sched_tz)
            if decode_error:
                sched_error = f"{limit_name} schedule {decode_error}"

        limits[limit_name] = ParsedBucketLimit(
            tc_delta=tc_delta,
            tk_milli=int(new_image.get(tk_attr, {}).get("N", "0")),
            cp_milli=int(new_image.get(cp_attr, {}).get("N", "0")),
            ra_milli=int(new_image.get(ra_attr, {}).get("N", "0")),
            rp_ms=int(new_image.get(rp_attr, {}).get("N", "0")),
            sched=limit_sched,
        )

    if not limits:
        return None

    if sched_error is not None:
        logger.warning(
            "Undecodable stored schedule - skipping refill for this bucket",
            entity_id=entity_id,
            resource=resource,
            shard_id=shard_id,
            reason=sched_error,
        )

    return ParsedBucketRecord(
        namespace_id=namespace_id,
        entity_id=entity_id,
        resource=resource,
        rf_ms=rf_ms,
        limits=limits,
        shard_id=shard_id,
        shard_count=shard_count,
        sched=sched,
        sched_compact=sched_compact,
        vu_ms=vu_ms,
        sched_error=sched_error,
    )


def extract_deltas(record: dict[str, Any]) -> list[ConsumptionDelta]:
    """
    Extract consumption deltas from a composite bucket stream record.

    With composite items (ADR-114), one stream event contains all limits for
    an entity+resource. This function enumerates b_{name}_tc attributes to
    extract one ConsumptionDelta per limit that changed.

    Args:
        record: DynamoDB stream record

    Returns:
        List of ConsumptionDeltas (one per limit with changed tc counter).
        Empty list if not a bucket record or no consumption changes.
    """
    parsed = _parse_bucket_record(record)
    if not parsed:
        return []

    deltas: list[ConsumptionDelta] = []
    for limit_name, info in parsed.limits.items():
        if limit_name == WCU_LIMIT_NAME:
            continue  # Internal infrastructure limit, not for usage snapshots
        if info.tc_delta == 0:
            continue
        deltas.append(
            ConsumptionDelta(
                namespace_id=parsed.namespace_id,
                entity_id=parsed.entity_id,
                resource=parsed.resource,
                limit_name=limit_name,
                tokens_delta=info.tc_delta,
                timestamp_ms=parsed.rf_ms,
            )
        )

    return deltas


def aggregate_bucket_states(
    records: list[dict[str, Any]],
) -> dict[tuple[str, str, str, int], BucketRefillState]:
    """Aggregate per-bucket state from stream records for refill decisions.

    For each (namespace_id, entity_id, resource, shard_id) composite bucket:
    - Accumulates ``tc`` deltas across all events per limit
    - Keeps the last NewImage's bucket fields (tk, cp, ra, rp) per limit
    - Keeps the last shared ``rf`` timestamp (optimistic lock target)

    Args:
        records: DynamoDB stream records

    Returns:
        Dict mapping (namespace_id, entity_id, resource, shard_id) to BucketRefillState
    """
    bucket_states: dict[tuple[str, str, str, int], BucketRefillState] = {}

    for record in records:
        if record.get("eventName") != "MODIFY":
            continue

        parsed = _parse_bucket_record(record)
        if not parsed:
            continue

        key = (parsed.namespace_id, parsed.entity_id, parsed.resource, parsed.shard_id)

        if key not in bucket_states:
            bucket_states[key] = BucketRefillState(
                namespace_id=parsed.namespace_id,
                entity_id=parsed.entity_id,
                resource=parsed.resource,
                rf_ms=parsed.rf_ms,
                shard_id=parsed.shard_id,
                shard_count=parsed.shard_count,
                sched=parsed.sched,
                sched_compact=parsed.sched_compact,
                vu_ms=parsed.vu_ms,
                sched_error=parsed.sched_error,
            )
        else:
            # Last NewImage wins, exactly as it does for rf/tk/cp below: stream
            # records for one key arrive in order, so the last image is the
            # closest thing to the item's current state.
            bucket_states[key].rf_ms = parsed.rf_ms
            bucket_states[key].sched = parsed.sched
            bucket_states[key].sched_compact = parsed.sched_compact
            bucket_states[key].vu_ms = parsed.vu_ms
            bucket_states[key].sched_error = parsed.sched_error

        state = bucket_states[key]

        for limit_name, parsed_limit in parsed.limits.items():
            if limit_name in state.limits:
                existing = state.limits[limit_name]
                existing.tc_delta += parsed_limit.tc_delta
                existing.tk_milli = parsed_limit.tk_milli
                existing.cp_milli = parsed_limit.cp_milli
                existing.ra_milli = parsed_limit.ra_milli
                existing.rp_ms = parsed_limit.rp_ms
                existing.sched = parsed_limit.sched
            else:
                state.limits[limit_name] = LimitRefillInfo(
                    tc_delta=parsed_limit.tc_delta,
                    tk_milli=parsed_limit.tk_milli,
                    cp_milli=parsed_limit.cp_milli,
                    ra_milli=parsed_limit.ra_milli,
                    rp_ms=parsed_limit.rp_ms,
                    sched=parsed_limit.sched,
                )

    return bucket_states


def _item_next_boundary(state: BucketRefillState, now_ms: int) -> int | None:
    """The earliest instant after ``now_ms`` where any schedule on the item changes.

    ``vu`` is one item-level attribute, so the *earliest* change anywhere on the
    item is what has to force the next materialising pass — the same ``min``
    rule the client slow path applies across a bucket's limits. Taking only the
    item-level default schedule would stamp a later ``vu`` whenever a per-limit
    override turns over first, and a late ``vu`` is the unsafe direction: it
    leaves the fast path admitting at the previous window's rate.

    Returns None when nothing on the item is scheduled.
    """
    scheds = {state.sched} | {info.sched for info in state.limits.values()}
    boundaries = [b for s in scheds if s and (b := next_boundary(s, now_ms=now_ms)) is not None]
    return min(boundaries) if boundaries else None


def try_refill_bucket(
    table: Any,
    state: BucketRefillState,
    now_ms: int,
) -> bool:
    """Try to refill a composite bucket if projected tokens are insufficient.

    For each limit in the bucket, computes the refill delta using
    ``refill_bucket()``.  A *positive* delta is only written if the limit's
    projected tokens (after natural refill) won't cover the observed
    consumption rate for the next batch window.  A *negative* delta — the
    bucket holds more than its effective cap after a shrink — is always
    written, ungated by that threshold.

    Scheduled limits (#222) are honoured from the bucket item's own ``sched``
    attributes, so the aggregator tops up toward the *scheduled* ceiling at the
    *scheduled* rate without reading config. An expired ``vu`` is replaced with
    the next boundary in the same ``rf``-locked write, which is what lets the
    fast path resume.

    Uses ``ADD`` for token deltas (commutative with concurrent speculative
    writes) and an optimistic lock on the shared ``rf`` timestamp to prevent
    double-refill with the slow path.

    Args:
        table: boto3 Table resource
        state: Aggregated bucket state from stream records
        now_ms: Current time (epoch milliseconds)

    Returns:
        True if a refill was written, False if skipped or lost the lock race
    """
    if not state.limits:
        return False

    if state.sched_error is not None:
        # Refilling at the *base* rate would silently undo a scale-down, so a
        # schedule we cannot read means we do not touch the bucket at all. The
        # client slow path still applies the operator's `on_unavailable`
        # setting to the same item (§6). Already logged at parse time.
        return False

    # Compute per-limit refill deltas
    add_parts: list[str] = []
    expr_values: dict[str, Any] = {}
    expr_names: dict[str, str] = {}
    any_needs_refill = False

    for limit_name, info in state.limits.items():
        if info.rp_ms <= 0 or info.ra_milli <= 0:
            continue

        if limit_name == WCU_LIMIT_NAME:
            # `wcu` is the per-partition DynamoDB write ceiling, not a user
            # limit: it is never divided by shard_count (every shard is its own
            # partition — `_deserialize_composite_bucket` and the Path 2 clone
            # below both say so) and a user's schedule must never scale it.
            effective_cp = info.cp_milli
            effective_ra = info.ra_milli
            effective_rp = info.rp_ms
        else:
            # Scale first, THEN divide by shard_count: the schedule applies to
            # the whole limit, the shard split to what is left of it.
            # `info.sched` is the item-level default unless the item carried a
            # per-limit override; falling back keeps a state assembled without
            # the per-limit copy (a hand-built one, or a future caller) from
            # silently refilling at the unscheduled base rate.
            scaled_cp, scaled_ra, effective_rp = effective_params(
                info.cp_milli, info.ra_milli, info.rp_ms, info.sched or state.sched, now_ms
            )
            effective_cp = scaled_cp // state.shard_count
            effective_ra = scaled_ra // state.shard_count

        result = refill_bucket(
            tokens_milli=info.tk_milli,
            last_refill_ms=state.rf_ms,
            now_ms=now_ms,
            capacity_milli=effective_cp,
            refill_amount_milli=effective_ra,
            refill_period_ms=effective_rp,
        )

        refill_delta = result.new_tokens_milli - info.tk_milli
        if refill_delta == 0:
            continue

        if refill_delta > 0:
            # Threshold: only top up if projected tokens < consumption for next
            # window. Use the accumulated tc delta as proxy for next-window
            # consumption.
            projected = result.new_tokens_milli
            consumption_estimate = max(0, info.tc_delta)
            if projected >= consumption_estimate:
                continue
        # A negative delta is a clamp: the bucket holds more than its effective
        # cap after a shrink (set_limits, a shard doubling, or a schedule
        # boundary). It bypasses the threshold above — a hot bucket has the
        # largest tc_delta, and that is exactly where a shrink most needs to
        # land, since the aggregator exists to keep the slow path from running
        # there at all. Safe as an ADD for the same commutativity reason the
        # positive case is: it removes exactly the surplus, and concurrent
        # consumption subtracts independently (#222 §3.3, replaces #469).

        any_needs_refill = True
        tk_attr = bucket_attr(limit_name, BUCKET_FIELD_TK)
        placeholder = f":rd_{limit_name}"
        add_parts.append(f"{tk_attr} {placeholder}")
        expr_values[placeholder] = refill_delta

    if not any_needs_refill:
        logger.debug(
            "Refill skipped - sufficient tokens",
            entity_id=state.entity_id,
            resource=state.resource,
        )
        return False

    # Build single UpdateItem for the composite bucket
    # ADD is commutative with concurrent speculative writes (Issue #317)
    set_parts = ["rf = :new_rf"]
    expr_values[":new_rf"] = now_ms
    expr_values[":expected_rf"] = state.rf_ms
    condition = "rf = :expected_rf"

    # #508: the `rf` lock alone cannot see a `_sync_bucket_params` fan-out.
    # That fan-out rewrites `cp`/`ra`/`rp`/`sched` on every shard and does NOT
    # touch `rf`, so a stream image captured before it passes the lock and
    # refills toward the *old, larger* capacity — above the limit `set_limits`
    # was called to impose, until the next materialising pass clamps it (#496).
    #
    # Pinning `vu` closes the whole class rather than one attribute: since
    # #222's Task 13 the fan-out SETs `vu = 0` on EVERY fan-out, scheduled or
    # not, so `vu` is the epoch marker for "the operator changed something
    # here" and covers every attribute that write touches, including ones
    # added later. It costs one condition term instead of the three-per-limit
    # that pinning cp/ra/rp individually would, and unlike bumping `rf` in the
    # fan-out it forfeits no accrued refill.
    #
    # It adds no false failures the `rf` lock was not already going to catch:
    # the only other writers of `vu` are the client slow path and this
    # function, and both move `rf` in the same write. PR #506 pinned `#sched`
    # for the narrower version of this race on the re-stamp path only; that
    # pin is kept below because it is what makes the *boundary* it computes
    # trustworthy, which is a different claim from "the item has not moved".
    expr_names["#vu"] = BUCKET_FIELD_VU
    if state.vu_ms is None:
        condition += " AND attribute_not_exists(#vu)"
    else:
        condition += " AND #vu = :expected_vu"
        expr_values[":expected_vu"] = state.vu_ms

    # An expired `vu` means this pass is the materialisation the fast path is
    # waiting on: stamp the next boundary so it can resume. A `vu` still in the
    # future is left alone — re-stamping it would move the gate the client is
    # already honouring.
    if state.vu_ms is not None and state.vu_ms <= now_ms:
        boundary = _item_next_boundary(state, now_ms)
        if boundary is not None:
            set_parts.append("#vu = :new_vu")
            expr_values[":new_vu"] = boundary
            # The stream image the boundary was computed from can be older than
            # the item: the #468 fan-out rewrites `sched`/`cp` and `vu = 0`
            # *without* touching `rf`, so the rf lock alone does not detect it.
            # Pinning the schedule keeps a pre-fan-out image from pushing `vu`
            # back into the future and cancelling the pass `vu = 0` forced.
            expr_names["#sched"] = BUCKET_FIELD_SCHED
            if state.sched_compact is None:
                condition += " AND attribute_not_exists(#sched)"
            else:
                condition += " AND #sched = :expected_sched"
                expr_values[":expected_sched"] = state.sched_compact

    update_expr = f"SET {', '.join(set_parts)} ADD {', '.join(add_parts)}"

    update_kwargs: dict[str, Any] = {
        "Key": {
            "PK": pk_bucket(state.namespace_id, state.entity_id, state.resource, state.shard_id),
            "SK": sk_state(),
        },
        "UpdateExpression": update_expr,
        "ConditionExpression": condition,
        "ExpressionAttributeValues": expr_values,
    }
    # Always non-empty since the #508 `vu` pin above, but kept as a guard:
    # DynamoDB rejects an ExpressionAttributeNames map with an unused entry,
    # and a future edit that drops the last alias would otherwise fail there.
    if expr_names:
        update_kwargs["ExpressionAttributeNames"] = expr_names

    try:
        table.update_item(**update_kwargs)

        logger.debug(
            "Bucket refilled",
            entity_id=state.entity_id,
            resource=state.resource,
            limits_refilled=list(name for name in state.limits if f":rd_{name}" in expr_values),
        )
        return True

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug(
                "Refill skipped - concurrent rf update",
                entity_id=state.entity_id,
                resource=state.resource,
            )
            return False
        raise


WCU_PROACTIVE_THRESHOLD_LOW = 0.2  # Shard when wcu tokens < 20% of capacity


def try_proactive_shard(
    table: Any,
    state: BucketRefillState,
    wcu_tk_milli: int,
    wcu_capacity_milli: int,
) -> bool:
    """Proactively double shard_count when wcu token level is low.

    Checks remaining wcu tokens against capacity. When tokens drop
    below 20% of capacity, the partition is under sustained write
    pressure and should be split.

    Only acts on shard 0 (source of truth for shard_count).
    Uses conditional write to prevent double-bumping.

    Args:
        table: boto3 Table resource
        state: Aggregated bucket state
        wcu_tk_milli: Remaining wcu tokens in millitokens (from last NewImage)
        wcu_capacity_milli: wcu capacity in millitokens

    Returns:
        True if shard_count was bumped, False otherwise
    """
    if state.shard_id != 0:
        return False

    if wcu_capacity_milli <= 0:
        return False

    token_ratio = wcu_tk_milli / wcu_capacity_milli
    if token_ratio >= WCU_PROACTIVE_THRESHOLD_LOW:
        return False

    new_count = state.shard_count * 2

    try:
        table.update_item(
            Key={
                "PK": pk_bucket(state.namespace_id, state.entity_id, state.resource, 0),
                "SK": sk_state(),
            },
            UpdateExpression="SET shard_count = :new",
            ConditionExpression="shard_count = :old",
            ExpressionAttributeValues={
                ":old": state.shard_count,
                ":new": new_count,
            },
        )
        logger.info(
            "Proactive shard doubling",
            entity_id=state.entity_id,
            resource=state.resource,
            old_count=state.shard_count,
            new_count=new_count,
            token_ratio=round(token_ratio, 2),
        )
        if new_count > WCU_SHARD_WARN_THRESHOLD:
            logger.warning(
                "High shard count after proactive doubling",
                entity_id=state.entity_id,
                resource=state.resource,
                shard_count=new_count,
                threshold=WCU_SHARD_WARN_THRESHOLD,
            )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug(
                "Proactive shard skipped - concurrent bump",
                entity_id=state.entity_id,
                resource=state.resource,
            )
            return False
        raise


def _extract_limit_attrs(
    image: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """Extract limit names and their bucket parameters from a stream image.

    Scans ``b_{name}_cp`` attributes to discover limits and their values.

    Returns:
        Dict of limit_name -> {"cp_milli", "tk_milli", "ra_milli", "rp_ms"}
    """
    limits: dict[str, dict[str, int]] = {}
    for attr_name in image:
        if not attr_name.startswith(BUCKET_ATTR_PREFIX):
            continue
        rest = attr_name[len(BUCKET_ATTR_PREFIX) :]
        idx = rest.rfind("_")
        if idx <= 0:
            continue
        if rest[idx + 1 :] != BUCKET_FIELD_CP:
            continue
        limit_name = rest[:idx]
        if not limit_name:
            continue

        limits[limit_name] = {
            field_key: int(image.get(bucket_attr(limit_name, field), {}).get("N", "0"))
            for field_key, field in (
                ("cp_milli", BUCKET_FIELD_CP),
                ("tk_milli", BUCKET_FIELD_TK),
                ("ra_milli", BUCKET_FIELD_RA),
                ("rp_ms", BUCKET_FIELD_RP),
            )
        }
    return limits


def propagate_shard_count(
    table: Any,
    record: dict[str, Any],
    now_ms: int | None = None,
) -> int:
    """Propagate shard_count changes to all other shard items.

    Detects shard_count change in stream record (OldImage vs NewImage).
    Only propagates from shard 0. Two code paths:
    - Existing shards (1..old_count-1): UpdateItem with shard_count < :new
    - New shards (old_count..new_count-1): PutItem cloned from shard 0's
      NewImage with adjusted PK/GSI keys and effective token capacity.
      Uses attribute_not_exists(PK) to avoid overwriting client-created items.

    Args:
        table: boto3 Table resource
        record: DynamoDB stream record
        now_ms: Current time (epoch milliseconds); read from the clock when
            omitted. Only used to evaluate the cloned item's schedule.

    Returns:
        Number of shard items created or updated
    """
    if now_ms is None:
        now_ms = int(time_module.time() * 1000)

    dynamodb_data = record.get("dynamodb", {})
    new_image = dynamodb_data.get("NewImage", {})
    old_image = dynamodb_data.get("OldImage", {})

    new_count_raw = new_image.get("shard_count", {}).get("N")
    old_count_raw = old_image.get("shard_count", {}).get("N")
    if not new_count_raw or not old_count_raw:
        return 0

    new_count = int(new_count_raw)
    old_count = int(old_count_raw)
    if new_count <= old_count:
        return 0

    pk = new_image.get("PK", {}).get("S", "")
    try:
        namespace_id, entity_id, resource, shard_id = parse_bucket_pk(pk)
    except ValueError:
        return 0

    if shard_id != 0:
        return 0  # Only propagate from source of truth

    updated = 0

    # Path 1: Update existing shards (lightweight shard_count update)
    for target_shard in range(1, old_count):
        try:
            table.update_item(
                Key={
                    "PK": pk_bucket(namespace_id, entity_id, resource, target_shard),
                    "SK": sk_state(),
                },
                UpdateExpression="SET shard_count = :new",
                ConditionExpression="shard_count < :new",
                ExpressionAttributeValues={
                    ":new": new_count,
                },
            )
            updated += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # Higher value already present
            raise

    # Path 2: Pre-create new shards (full item cloned from shard 0)
    # Deserialize wire format ({"S": "val"}, {"N": "1"}) to Python types
    # because table.put_item() (boto3 Table resource) auto-serializes.
    deserializer = TypeDeserializer()
    base_item = {k: deserializer.deserialize(v) for k, v in new_image.items()}
    limit_attrs = _extract_limit_attrs(new_image)

    # A cloned shard is created *full*, so it must be filled to the ceiling in
    # force right now — the scheduled one, not the base (#222). Cloning the
    # base during a 0.5x window would hand the new shard twice the tokens the
    # schedule allows, and the item carries shard 0's `vu`, which is in the
    # future, so the fast path would spend them before any pass trimmed it.
    sched_tz = new_image.get(BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    item_sched, sched_error = _decode_schedule(
        new_image.get(BUCKET_FIELD_SCHED, {}).get("S"), sched_tz
    )
    # Per-limit starting balance, computed once rather than per target shard.
    starting_tokens: dict[str, int] = {}
    for limit_name, info in limit_attrs.items():
        if limit_name == WCU_LIMIT_NAME:
            starting_tokens[limit_name] = info["cp_milli"]  # per-partition, not divided
            continue
        limit_compact = new_image.get(
            f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_SCHED}", {}
        ).get("S")
        limit_sched, limit_error = _decode_schedule(limit_compact, sched_tz)
        error = sched_error or limit_error
        if error is not None:
            logger.warning(
                "Undecodable stored schedule - not pre-creating shards",
                entity_id=entity_id,
                resource=resource,
                limit_name=limit_name,
                reason=error,
            )
            return updated
        scaled_cp, _ra, _rp = effective_params(
            info["cp_milli"],
            info["ra_milli"],
            info["rp_ms"],
            limit_sched or item_sched,
            now_ms,
        )
        starting_tokens[limit_name] = scaled_cp // new_count

    for target_shard in range(old_count, new_count):
        try:
            item = dict(base_item)
            item["PK"] = pk_bucket(namespace_id, entity_id, resource, target_shard)
            item["GSI2SK"] = gsi2_sk_bucket(entity_id, target_shard)
            item["GSI3SK"] = gsi3_sk_bucket(resource, target_shard)
            item["GSI4SK"] = gsi4_sk_bucket(entity_id, resource, target_shard)
            item["shard_count"] = new_count
            # Reset tokens to effective per-shard capacity (full bucket)
            for limit_name, effective_cp in starting_tokens.items():
                item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = effective_cp
                item[bucket_attr(limit_name, BUCKET_FIELD_TC)] = 0
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            updated += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # Client already created this shard
            raise

    if updated > 0:
        logger.info(
            "Shard count propagated",
            entity_id=entity_id,
            resource=resource,
            new_count=new_count,
            shards_updated=updated,
        )
    return updated


def get_window_key(timestamp_ms: int, window: str) -> str:
    """
    Get the window key (ISO timestamp) for a given timestamp.

    Args:
        timestamp_ms: Epoch milliseconds
        window: Window type ("hourly", "daily", "monthly")

    Returns:
        ISO timestamp string for the window start
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)

    if window == "hourly":
        return dt.strftime("%Y-%m-%dT%H:00:00Z")
    elif window == "daily":
        return dt.strftime("%Y-%m-%dT00:00:00Z")
    elif window == "monthly":
        return dt.strftime("%Y-%m-01T00:00:00Z")
    else:
        raise ValueError(f"Unknown window type: {window}")


def get_window_end(window_key: str, window: str) -> str:
    """
    Get the window end timestamp.

    Args:
        window_key: Window start (ISO timestamp)
        window: Window type

    Returns:
        ISO timestamp string for the window end
    """
    dt = datetime.fromisoformat(window_key.replace("Z", "+00:00"))

    if window == "hourly":
        end_dt = dt.replace(minute=59, second=59)
    elif window == "daily":
        end_dt = dt.replace(hour=23, minute=59, second=59)
    elif window == "monthly":
        # Last day of month
        if dt.month == 12:
            end_dt = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
        else:
            end_dt = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
    else:
        end_dt = dt

    return end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def calculate_snapshot_ttl(ttl_days: int) -> int:
    """Calculate TTL epoch seconds."""
    return int(datetime.now(UTC).timestamp()) + (ttl_days * 86400)


def update_snapshot(
    table: Any,
    delta: ConsumptionDelta,
    window: str,
    ttl_days: int,
) -> None:
    """
    Update a usage snapshot record atomically.

    Uses DynamoDB ADD operation to increment counters, creating
    the record if it doesn't exist. Uses a FLAT schema (no nested
    data map) to enable atomic upsert with ADD operations in a
    single DynamoDB call.

    Args:
        table: boto3 Table resource
        delta: Consumption delta to record
        window: Window type
        ttl_days: TTL in days
    """
    window_key = get_window_key(delta.timestamp_ms, window)

    # Convert millitokens to tokens for storage
    tokens_delta = delta.tokens_delta // 1000

    # Build update expression using FLATTENED schema (no nested data map).
    #
    # Snapshots use a flat structure unlike other record types (entities, buckets)
    # which use nested data.M maps. This is because snapshots require atomic upsert
    # with ADD counters, and DynamoDB has a limitation: you cannot SET a map path
    # (#data = if_not_exists(...)) AND ADD to paths within it (#data.counter) in
    # the same expression - it fails with "overlapping document paths" error.
    #
    # The flat structure allows a single atomic update_item call that:
    # - Creates the item if it doesn't exist (SET with if_not_exists for metadata)
    # - Atomically increments counters (ADD for limit consumption and event count)
    #
    # See: https://github.com/zeroae/zae-limiter/issues/168
    table.update_item(
        Key={
            "PK": pk_entity(delta.namespace_id, delta.entity_id),
            "SK": sk_usage(delta.resource, window_key),
        },
        UpdateExpression="""
            SET entity_id = :entity_id,
                #resource = if_not_exists(#resource, :resource),
                #window = if_not_exists(#window, :window),
                #window_start = if_not_exists(#window_start, :window_start),
                GSI2PK = :gsi2pk,
                GSI2SK = :gsi2sk,
                GSI4PK = if_not_exists(GSI4PK, :gsi4pk),
                GSI4SK = if_not_exists(GSI4SK, :gsi4sk),
                #ttl = if_not_exists(#ttl, :ttl)
            ADD #limit_name :delta,
                #total_events :one
        """,
        ExpressionAttributeNames={
            "#resource": "resource",
            "#window": "window",
            "#window_start": "window_start",
            "#limit_name": delta.limit_name,
            "#total_events": "total_events",
            "#ttl": "ttl",
        },
        ExpressionAttributeValues={
            ":entity_id": delta.entity_id,
            ":resource": delta.resource,
            ":window": window,
            ":window_start": window_key,
            ":gsi2pk": gsi2_pk_resource(delta.namespace_id, delta.resource),
            ":gsi2sk": gsi2_sk_usage(window_key, delta.entity_id),
            ":gsi4pk": delta.namespace_id,
            ":gsi4sk": pk_entity(delta.namespace_id, delta.entity_id),
            ":ttl": calculate_snapshot_ttl(ttl_days),
            ":delta": tokens_delta,
            ":one": 1,
        },
    )

    logger.debug(
        "Snapshot updated",
        entity_id=delta.entity_id,
        resource=delta.resource,
        limit_name=delta.limit_name,
        window=window,
        window_key=window_key,
        tokens_delta=tokens_delta,
    )
