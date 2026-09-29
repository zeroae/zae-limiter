"""DynamoDB Stream processor for usage aggregation and bucket refill."""

import json
import time as time_module
import traceback
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

from zae_limiter.bucket import refill_bucket
from zae_limiter.models import (
    QuotaDonorDebit,
    QuotaSibling,
    is_accrual_rate,
    plan_quota_grant,
    quota_period_is_current,
)
from zae_limiter.schedule import (
    ScheduleEntry,
    decode,
    decode_reset,
    effective_params,
    next_boundary,
    prev_reset_edge,
)
from zae_limiter.schema import (
    BUCKET_ATTR_PREFIX,
    BUCKET_FIELD_CP,
    BUCKET_FIELD_GC,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_RSA,
    BUCKET_FIELD_RSCHED,
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_SCHED_TZ,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_VU,
    BUCKET_FIELD_WA,
    BUCKET_FIELD_WS,
    BUCKET_FIELD_WTC,
    BUCKET_PREFIX,
    BUCKET_SCHED_NONE,
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
    # Reset schedule in force for *this* limit (#222 §3.6), resolved the same
    # way from `rsched` / `b_{name}_rsched`. Empty means the balance only ever
    # drips back.
    reset_sched: tuple[ScheduleEntry, ...] = ()
    # Duration window (ADR-139): `b_{name}_ws` (window start, epoch ms) and
    # `b_{name}_rsa` (window length, seconds). Per-limit only — there is no
    # item-level default to inherit. None when the attribute is absent.
    window_start_ms: int | None = None
    reset_after_seconds: int | None = None
    # `b_{name}_wa` (#640): the window start this shard's balance reflects.
    # None on an item written before the marker, where `_window_applied`
    # falls back to the item's `rf`.
    window_applied_ms: int | None = None
    # `b_{name}_tc` as of the last NewImage (absolute, not the delta above) and
    # `b_{name}_wtc`, the fan-out's snapshot of it (#640). Together they give a
    # pending roll's target, `eff_cp - max(0, tc - wtc)`.
    tc_milli: int | None = None
    window_consumed_mark_milli: int | None = None
    # `b_{name}_gc` (ADR-145): the shard count this shard's current-period
    # quota grant was sized at. None on an item written before ADR-145.
    grant_count: int | None = None


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
    # Item-level default reset schedule (#222 §3.6). Applies to every limit on
    # the item that carries no `b_{name}_rsched` override of its own — except
    # `wcu`, which is exempted where it is read.
    reset_sched: tuple[ScheduleEntry, ...] = ()
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

    # No early return on an empty `deltas`, deliberately. Usage aggregation was
    # this function's only job when that short-circuit was written; refill
    # (#317), the negative clamp and reset edge (#222 §3.3/§3.6), the `vu`
    # re-stamp (§2.1) and proactive sharding all landed *below* it afterwards,
    # and every one of them reads the bucket image rather than a consumption
    # delta. Returning here skipped all of them for any batch that carried no
    # consumption — which is exactly the batch a `_sync_bucket_params` fan-out
    # produces: it rewrites `cp`/`ra`/`sched` and stamps `vu = 0` without
    # touching `tc`. The bucket was then left above its new ceiling with `vu`
    # expired, pinned to the client slow path, until some client happened to
    # acquire against it. A shard_count propagation and an ADR-125 disable
    # stamp have the same shape.
    #
    # The snapshot loop below is a no-op on an empty list, so the cost of
    # falling through is `aggregate_bucket_states()` over records that parse to
    # nothing.

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
    reset_sched: tuple[ScheduleEntry, ...] = ()  # per-limit reset schedule (#222 §3.6)
    window_start_ms: int | None = None  # b_{name}_ws, epoch ms (ADR-139)
    reset_after_seconds: int | None = None  # b_{name}_rsa, seconds (ADR-139)
    window_applied_ms: int | None = None  # b_{name}_wa, epoch ms (#640)
    tc_milli: int | None = None  # b_{name}_tc from NewImage, absolute (#640)
    window_consumed_mark_milli: int | None = None  # b_{name}_wtc (#640)
    grant_count: int | None = None  # b_{name}_gc, shard's grant sizing (ADR-145)


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
    reset_sched: tuple[ScheduleEntry, ...] = ()  # item-level default reset schedule (§3.6)
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


def _decode_reset_schedule(
    compact: str | None, tz: str
) -> tuple[tuple[ScheduleEntry, ...], str | None]:
    """Decode a stored compact *reset* schedule into ``(schedule, error)``.

    Reported rather than raised, for the identical reason
    :func:`_decode_schedule` is: ``aggregate_bucket_states`` runs outside any
    try block, so a raise here is a poison pill for the whole batch — usage
    snapshots included. The error folds into the same ``sched_error`` channel,
    which makes the bucket untouchable rather than resettable-at-the-base.
    """
    if not compact:
        return (), None
    try:
        return decode_reset(compact, tz), None
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

    # The reset schedule rides in its own attribute pair and shares `sched_tz`
    # (§4.1), so it decodes the same way and fails into the same channel.
    rsched_compact = new_image.get(BUCKET_FIELD_RSCHED, {}).get("S")
    reset_sched, reset_decode_error = _decode_reset_schedule(rsched_compact, sched_tz)
    if reset_decode_error and sched_error is None:
        sched_error = f"item reset schedule {reset_decode_error}"

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
        # tighter of the two. `BUCKET_SCHED_NONE` is the override that says
        # "this limit has none" (#541): absence still means "inherit", so
        # without an explicit spelling an unscheduled limit beside a scheduled
        # one is refilled toward `0.5 x capacity` at `0.5 x` its rate.
        limit_sched = sched
        sched_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_SCHED}"
        limit_compact = new_image.get(sched_attr, {}).get("S")
        if limit_compact == BUCKET_SCHED_NONE:
            limit_sched = ()
        elif limit_compact and limit_compact != sched_compact:
            limit_sched, decode_error = _decode_schedule(limit_compact, sched_tz)
            if decode_error:
                sched_error = f"{limit_name} schedule {decode_error}"

        # `b_{name}_rsched` overrides the item-level reset default for this
        # limit only, mirroring `b_{name}_sched` above — the `NONE` marker
        # included, and it matters more here: an inherited reset hard-SETs a
        # balance on a calendar the limit never declared.
        limit_reset_sched = reset_sched
        rsched_attr = f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_RSCHED}"
        limit_rcompact = new_image.get(rsched_attr, {}).get("S")
        if limit_rcompact == BUCKET_SCHED_NONE:
            limit_reset_sched = ()
        elif limit_rcompact and limit_rcompact != rsched_compact:
            limit_reset_sched, reset_decode_error = _decode_reset_schedule(limit_rcompact, sched_tz)
            if reset_decode_error:
                sched_error = f"{limit_name} reset schedule {reset_decode_error}"

        # The duration window (ADR-139). Integers rather than a compact
        # grammar, so unlike `sched` / `rsched` there is no decode step and no
        # `sched_error` analogue: nothing here can poison the batch that a
        # malformed `tc` could not already (and the per-record guard in
        # `aggregate_bucket_states` covers that). Per-limit only — `ws` has no
        # item-level default — which is also why `wcu` is exempt structurally
        # rather than by a carve-out: no writer ever stamps one on it.
        ws_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_WS), {}).get("N")
        rsa_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_RSA), {}).get("N")
        wa_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_WA), {}).get("N")
        wtc_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_WTC), {}).get("N")
        gc_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_GC), {}).get("N")

        limits[limit_name] = ParsedBucketLimit(
            tc_delta=tc_delta,
            tk_milli=int(new_image.get(tk_attr, {}).get("N", "0")),
            cp_milli=int(new_image.get(cp_attr, {}).get("N", "0")),
            ra_milli=int(new_image.get(ra_attr, {}).get("N", "0")),
            rp_ms=int(new_image.get(rp_attr, {}).get("N", "0")),
            sched=limit_sched,
            reset_sched=limit_reset_sched,
            window_start_ms=int(ws_raw) if ws_raw is not None else None,
            reset_after_seconds=int(rsa_raw) if rsa_raw is not None else None,
            window_applied_ms=int(wa_raw) if wa_raw is not None else None,
            tc_milli=int(new_tc_raw),
            window_consumed_mark_milli=int(wtc_raw) if wtc_raw is not None else None,
            grant_count=int(gc_raw) if gc_raw is not None else None,
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
        reset_sched=reset_sched,
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

    for idx, record in enumerate(records):
        if record.get("eventName") != "MODIFY":
            continue

        # Per record, not per batch. `_parse_bucket_record` reads attributes
        # straight off the stream image and raises on a malformed one (a `tc`
        # that is not a number, say); letting that out of here would fail the
        # whole invocation, and the event source would redrive the same batch
        # until it aged out — the poison-pill shape the `extract_deltas` loop
        # above already guards against one record at a time, and that core
        # plan Task 14 closed for an undecodable schedule. Every other reader
        # of these attributes fails at item granularity; so does this one.
        try:
            parsed = _parse_bucket_record(record)
        except Exception as exc:
            logger.warning(
                f"Skipping unparseable bucket record: {exc}",
                exc_info=True,
                record_index=idx,
            )
            continue
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
                reset_sched=parsed.reset_sched,
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
            bucket_states[key].reset_sched = parsed.reset_sched
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
                existing.reset_sched = parsed_limit.reset_sched
                existing.window_start_ms = parsed_limit.window_start_ms
                existing.reset_after_seconds = parsed_limit.reset_after_seconds
                existing.window_applied_ms = parsed_limit.window_applied_ms
                existing.tc_milli = parsed_limit.tc_milli
                existing.window_consumed_mark_milli = parsed_limit.window_consumed_mark_milli
                existing.grant_count = parsed_limit.grant_count
            else:
                state.limits[limit_name] = LimitRefillInfo(
                    tc_delta=parsed_limit.tc_delta,
                    tk_milli=parsed_limit.tk_milli,
                    cp_milli=parsed_limit.cp_milli,
                    ra_milli=parsed_limit.ra_milli,
                    rp_ms=parsed_limit.rp_ms,
                    sched=parsed_limit.sched,
                    reset_sched=parsed_limit.reset_sched,
                    window_start_ms=parsed_limit.window_start_ms,
                    reset_after_seconds=parsed_limit.reset_after_seconds,
                    window_applied_ms=parsed_limit.window_applied_ms,
                    tc_milli=parsed_limit.tc_milli,
                    window_consumed_mark_milli=parsed_limit.window_consumed_mark_milli,
                    grant_count=parsed_limit.grant_count,
                )

    return bucket_states


def _window_in_force(limit_name: str, info: LimitRefillInfo) -> tuple[int | None, int] | None:
    """``(ws, rsa)`` for a limit whose duration window is in force, else None (ADR-139).

    ``rsa`` is what says a window is in force: every client write that stamps
    ``ws`` stamps ``rsa`` beside it, and the param sync REMOVEs ``rsa`` (never
    ``ws``) when a limit loses its window, so a ``ws`` with no ``rsa`` is a
    stale start that no client applies either (``_monotonic_rf`` lets only
    windows in force vote). ``ws`` may still be None beside an ``rsa``: the
    param sync stamped the length on a bucket whose first window has not been
    anchored yet.

    Two exclusions, both structural rather than behavioural:

    - ``wcu`` never carries a window — no writer stamps one on it — and is
      excluded anyway so a corrupt item cannot reset the per-partition write
      ceiling.
    - A limit carrying a ``reset_schedule`` as well is a corrupt item
      (``Limit`` makes the two spellings of the reset half mutually
      exclusive). The calendar branch keeps it, exactly as it did before
      windows existed, rather than two reset rules racing over one balance.
    """
    if limit_name == WCU_LIMIT_NAME or info.reset_after_seconds is None or info.reset_sched:
        return None
    return info.window_start_ms, info.reset_after_seconds


def _window_applied(info: LimitRefillInfo, rf_ms: int) -> int:
    """The window start this shard's balance reflects (#640).

    The aggregator's statement of :attr:`BucketState.window_rolled`: a window
    is unapplied when ``ws`` is newer than this. The limit's own ``wa`` marker
    when the item carries one, else the item's ``rf`` — the ADR-140 rule an
    item written before the marker was written under. The marker exists
    because a writer predating ADR-139 stamps ``rf`` from its own clock, and
    ``rf`` then says nothing about which window a balance belongs to.
    """
    return info.window_applied_ms if info.window_applied_ms is not None else rf_ms


def _item_next_boundary(state: BucketRefillState, now_ms: int) -> int | None:
    """The earliest instant after ``now_ms`` where any schedule on the item changes.

    ``vu`` is one item-level attribute, so the *earliest* change anywhere on the
    item is what has to force the next materialising pass — the same ``min``
    rule the client slow path applies across a bucket's limits. Taking only the
    item-level default schedule would stamp a later ``vu`` whenever a per-limit
    override turns over first, and a late ``vu`` is the unsafe direction: it
    leaves the fast path admitting at the previous window's rate.

    Both tuples vote. A limit carrying a ``reset_schedule`` and **no**
    ``schedule`` is the daily-quota shape (ADR-137), and with the parameter
    tuple alone it would produce no boundary at all: ``vu`` would be left
    unstamped, the fast path would never yield, and the reset would fire only
    when something unrelated forced a materialising pass (#222 §3.6).

    Returns None when nothing on the item is scheduled.

    The item-level pair is a member in its own right, not a fall-back for the
    per-limit ones (#541). A limit recorded as explicitly unscheduled
    contributes no boundary of its own, but the default still does — some limit
    on the item carries it, and `vu` is one item-level attribute, so the
    earliest change *anywhere* is the one that has to force the pass.

    Duration windows (ADR-139) are the third voting member, exactly as on the
    client (``RateLimiter._materialisation_stamps``): each window in force
    contributes its end, ``ws + rsa * 1000``. Leaving it out would let a
    re-stamp push ``vu`` past the window's end, and the fast path — which
    evaluates nothing — would keep spending the old window's balance after it
    closed, where the client means the first use past the end to take the slow
    path and anchor the next window. The #541 "item-level pair votes in its
    own right" reasoning does not apply: ``ws`` has no item-level default, so
    only the per-limit values vote.

    Unlike the cron members, a window's contribution can already lie at or
    before ``now_ms``: a window that has ended, or an ``rsa`` with no ``ws``
    (a window due to open on the next client pass). Either means the fast
    path must stay closed until a client anchors, so the result is then
    ``<= now_ms`` and the caller must not re-stamp ``vu`` at all.
    """
    pairs = {(state.sched, state.reset_sched)} | {
        (info.sched, info.reset_sched) for info in state.limits.values()
    }
    boundaries = [
        b
        for sched, reset in pairs
        if (sched or reset) and (b := next_boundary(sched, reset, now_ms=now_ms)) is not None
    ]
    for limit_name, info in state.limits.items():
        window = _window_in_force(limit_name, info)
        if window is None:
            continue
        ws, rsa = window
        boundaries.append(now_ms if ws is None else ws + rsa * 1000)
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

    windows = {
        name: window
        for name, info in state.limits.items()
        if (window := _window_in_force(name, info)) is not None
    }

    # A window this shard has not applied (`ws > wa`, ADR-140) that has also
    # already ended at `now`. The aggregator must not apply it: the balance
    # it would restore belongs to a window that is over, and the client's next
    # pass opens a *new* window (`_open_window_if_elapsed`) and resets
    # unconditionally. Anything spent from a late-restored dead window in the
    # gap — the consumption-only retry after a lost `rf` lock stamps no `ws`,
    # so it can spend it — would be admitted on top of the new window's full
    # allowance. Two allowances inside one window's span is over-admission.
    #
    # Nor may it write at all: `rf` would have to move past `ws` (the rf rule
    # below), which records the window as applied when it was not — and a
    # write that held `rf` below `ws` would stamp a drip refill computed to
    # `now` against an `rf` in the past, crediting the same interval twice on
    # the next pass. So the whole item is skipped. That costs nothing that
    # matters: `vu` on such an item is at or before the window's end, so the
    # fast path is already closed and the next acquire takes the slow path,
    # which refills every limit on the item itself.
    #
    # "Ended" is judged by this Lambda's own clock, which can run behind the
    # clients'. A skewed judgement is made harmless by the `ws` pin on the
    # roll below, not by this check: a window this pass wrongly believes is
    # live can only be restored if the item still carries that same `ws`.
    for name, (ws, rsa) in windows.items():
        applied = _window_applied(state.limits[name], state.rf_ms)
        if ws is not None and ws > applied and ws + rsa * 1000 <= now_ms:
            logger.debug(
                "Refill skipped - an unapplied duration window has already ended",
                entity_id=state.entity_id,
                resource=state.resource,
                limit_name=name,
            )
            return False

    # Compute per-limit refill deltas
    add_parts: list[str] = []
    expr_values: dict[str, Any] = {}
    expr_names: dict[str, str] = {}
    any_needs_refill = False
    rolled: list[str] = []
    # Quotas this write re-grants (ADR-145 I3): a reset edge or a window roll
    # it applies, whether or not the balance needed a delta. Each gets
    # `gc = shard_count` and the write is pinned on `shard_count` (I4).
    granted: list[str] = []
    # Limits given a reset or drip delta, in token order: `refilled[i]` is the
    # limit behind `#rt{i}` / `:rd{i}`. Positional tokens, never the limit
    # name (#634) — `NAME_PATTERN` allows `.` and `-`, and an inline
    # `b_rpm.v2_tk` parses as a nested document path.
    refilled: list[str] = []

    for limit_name, info in state.limits.items():
        if limit_name == WCU_LIMIT_NAME:
            # `wcu` is the per-partition DynamoDB write ceiling, not a user
            # limit: it is never divided by shard_count (every shard is its own
            # partition — `_deserialize_composite_bucket` and the Path 2 clone
            # below both say so) and a user's schedule must never scale it.
            effective_cp = info.cp_milli
            ceiling_cp = info.cp_milli
            effective_ra = info.ra_milli
            effective_rp = info.rp_ms
            # `rsched` is item-level and applies to every limit on the item by
            # default, so without this a user's daily reset would hand `wcu`
            # its ceiling back on every batch that crossed midnight. The client
            # gets the same exemption for free: `wcu` rides as a carrier built
            # by `Limit._carrier()`, which never sets `reset_schedule`.
            reset_sched: tuple[ScheduleEntry, ...] = ()
        else:
            # Scale first, THEN divide by shard_count: the schedule applies to
            # the whole limit, the shard split to what is left of it.
            #
            # `info.sched` is authoritative and there is deliberately no
            # fall-back to `state.sched` (#541): the parser already resolves
            # every limit against the item default, so an empty tuple here
            # means the limit is *explicitly* unscheduled — `b_{name}_sched`
            # held `BUCKET_SCHED_NONE` — and falling back would hand it the
            # window it was recorded as not having.
            scaled_cp, scaled_ra, effective_rp = effective_params(
                info.cp_milli, info.ra_milli, info.rp_ms, info.sched, now_ms
            )
            effective_cp = scaled_cp // state.shard_count
            effective_ra = scaled_ra // state.shard_count
            reset_sched = info.reset_sched
            # ADR-145 I7: a quota shard holds every slot its grant covers, so
            # its ceiling is `C // gc`, never `C // shard_count` (#637). The
            # reset / roll target stays `effective_cp`: a re-grant is sized
            # at the item's count. A dripping limit keeps `C // shard_count`.
            is_quota = bool(reset_sched) or info.reset_after_seconds is not None
            grant_count = info.grant_count
            if not is_quota or grant_count is None or grant_count < 1:
                grant_count = state.shard_count
            ceiling_cp = scaled_cp // grant_count

        # A duration window rolled since this item was last refilled sets the
        # balance to the effective capacity (ADR-140), which as an `ADD` is
        # `eff_cp - tk_observed` — the identical delta shape the reset branch
        # below and the unconditional clamp use, and safe for the identical
        # commutativity reason. It is the same `ws > wa` comparison (`ws > rf`
        # on an item without the #640 marker), against the same stored marker,
        # that `BucketState.window_rolled` makes on the client, so whichever
        # writer gets there first records `ws` as applied (and moves `rf`, the
        # lock) and the other one skips. A window that has already ended never
        # reaches here (the whole item was skipped above).
        #
        # Evaluated **before** the accrual-rate guard further down, not after
        # it: a duration quota's stored rate is 0 since ADR-137, so that guard
        # would skip exactly the limits this exists for.
        #
        # It applies a window the CLIENT anchored; it never anchors one. The
        # aggregator acts only on stream records, and an exhausted quota
        # produces none (a fast rejection is 0 WCU), so it could not anchor for
        # an idle entity even if it tried — which is correct, since the window
        # must be anchored to a *use*. And it does not fan out: it processes
        # one shard per record and would issue S² writes per batch rather than
        # S. The client's fan-out plus `ws > wa` already converges every shard.
        #
        # Positional aliases, never the limit name: `NAME_PATTERN` allows `-`
        # and `.`, neither legal in an expression token or an inline path.
        window = windows.get(limit_name)
        if (
            window is not None
            and window[0] is not None
            and window[0] > _window_applied(info, state.rf_ms)
        ):
            # The share less what the shard spent since the fan-out's snapshot
            # (#640) — the client's `BucketState.window_roll_target_milli` —
            # so debits made while the roll was pending, and already in this
            # image, are charged rather than forgiven by the SET.
            target = effective_cp
            if info.window_consumed_mark_milli is not None and info.tc_milli is not None:
                target -= max(0, info.tc_milli - info.window_consumed_mark_milli)
            roll_delta = target - info.tk_milli
            granted.append(limit_name)
            if roll_delta != 0:
                any_needs_refill = True
                idx = len(rolled)
                add_parts.append(f"#wtk{idx} :wd{idx}")
                expr_names[f"#wtk{idx}"] = bucket_attr(limit_name, BUCKET_FIELD_TK)
                expr_values[f":wd{idx}"] = roll_delta
                # Pin the window being restored (see the condition below).
                expr_names[f"#wws{idx}"] = bucket_attr(limit_name, BUCKET_FIELD_WS)
                expr_values[f":ews{idx}"] = window[0]
                rolled.append(limit_name)
            continue

        # A reset edge crossed since this item was last refilled sets the
        # balance to the effective capacity, which as an `ADD` is
        # `eff_cp - tk_observed` — the identical delta shape the unconditional
        # clamp uses, and safe for the identical commutativity reason: it
        # removes exactly the surplus (or adds exactly the shortfall) while
        # concurrent consumption subtracts independently. It is the same
        # `> rf` comparison, against the same stored `rf`, that
        # `RateLimiter._apply_reset_edge()` makes on the client, so whichever
        # writer gets there first stamps `rf` past the edge and the other one
        # skips — they agree by construction rather than by coincidence.
        #
        # Evaluated **before** the accrual-rate guard below, not after it: a
        # quota's stored rate is 0 since ADR-137, so that guard would skip
        # exactly the limits a reset exists for. It also bypasses the
        # consumption threshold further down, for the same reason the negative
        # clamp does — a hot bucket has the largest `tc_delta` and is precisely
        # where the aggregator, not the client, is the refiller (§3.3, §3.6).
        if reset_sched:
            reset_edge = prev_reset_edge(reset_sched, now_ms)
            if reset_edge is not None and reset_edge > state.rf_ms:
                reset_delta = effective_cp - info.tk_milli
                granted.append(limit_name)
                if reset_delta != 0:
                    any_needs_refill = True
                    idx = len(refilled)
                    add_parts.append(f"#rt{idx} :rd{idx}")
                    expr_names[f"#rt{idx}"] = bucket_attr(limit_name, BUCKET_FIELD_TK)
                    expr_values[f":rd{idx}"] = reset_delta
                    refilled.append(limit_name)
                continue

        # A stored rate that is not an accrual rate has nothing to refill. That
        # is the *normal* state of a quota since ADR-137 — it recovers only at
        # the `reset_schedule` edge handled above — and otherwise means a
        # corrupt or unparsed item. Either way, skipping is right:
        # `refill_bucket` would add nothing and the `rp_ms` half of the guard
        # exists to keep its drift division off a zero denominator.
        if info.rp_ms <= 0 or not is_accrual_rate(info.ra_milli):
            continue

        result = refill_bucket(
            tokens_milli=info.tk_milli,
            last_refill_ms=state.rf_ms,
            now_ms=now_ms,
            capacity_milli=ceiling_cp,
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
        idx = len(refilled)
        add_parts.append(f"#rt{idx} :rd{idx}")
        expr_names[f"#rt{idx}"] = bucket_attr(limit_name, BUCKET_FIELD_TK)
        expr_values[f":rd{idx}"] = refill_delta
        refilled.append(limit_name)

    if not any_needs_refill:
        logger.debug(
            "Refill skipped - sufficient tokens",
            entity_id=state.entity_id,
            resource=state.resource,
        )
        return False

    # Build single UpdateItem for the composite bucket
    # ADD is commutative with concurrent speculative writes (Issue #317)
    #
    # `rf` is stamped exactly as the client stamps it (`lease._monotonic_rf`,
    # ADR-140): `max(now, stored rf, every window start in force on the item)`.
    # On an item without the `wa` marker a window rolls when `ws > rf`, so
    # there `rf` is the only record that a shard has applied its window. An
    # aggregator clock behind the one that stamped the item would otherwise
    # move `rf` backward past `ws`, and the next pass would reset the balance
    # again — refunding everything spent since. And every unapplied `ws`
    # still on the item at this point is a live window the
    # loop above has applied (or found already at the effective capacity), so
    # stamping `rf` past it records a roll that really happened. The cost is
    # the one the client already accepts: `refill_bucket` treats a
    # non-positive elapsed time as zero, so an `rf` ahead of a later reader's
    # clock grants nothing rather than a negative refill.
    new_rf = max([now_ms, state.rf_ms] + [ws for ws, _rsa in windows.values() if ws is not None])
    set_parts = ["rf = :new_rf"]
    expr_values[":new_rf"] = new_rf
    expr_values[":expected_rf"] = state.rf_ms
    condition = "rf = :expected_rf"

    # #640: every window in force leaves this write reflecting the `ws` the
    # image carried — the loop above rolled it, or it was already applied — so
    # that value is stamped as the limit's window-applied marker. This is what
    # marks an item written before the marker, and what keeps a later `rf`
    # stamped by a writer predating ADR-139 from re-rolling (backward) or
    # hiding (forward) a window. The image's value, never a copy of the `ws`
    # path: a fan-out can move `ws` without touching `rf`, and that window must
    # stay unapplied. No new condition term — `wa` only ever moves under the
    # `rf` lock this write already holds, or at creation.
    marked = [
        (name, ws)
        for name, (ws, _rsa) in sorted(windows.items())
        if ws is not None and state.limits[name].window_applied_ms != ws
    ]
    for idx, (name, ws) in enumerate(marked):
        set_parts.append(f"#wa{idx} = :wa{idx}")
        expr_names[f"#wa{idx}"] = bucket_attr(name, BUCKET_FIELD_WA)
        expr_values[f":wa{idx}"] = ws

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

    # Each window this write restores is pinned to the `ws` the image carried.
    # The `rf` and `vu` pins cannot tell two window fan-outs apart: a client
    # that opens the *next* window on another shard fans it out with
    # `SET ws, rsa, vu = 0`, which leaves `rf` alone and rewrites `vu` to the
    # same 0 an earlier fan-out left. An aggregator whose clock runs behind
    # would then restore the dead window's balance under an `rf` still below
    # the new `ws` — spendable by the consumption-only retry before the next
    # pass rolls the new window in full, one share of over-admission. With
    # the pin that write fails its condition and is skipped like any other
    # lost lock. One term per rolled limit, positional aliases only.
    for idx in range(len(rolled)):
        condition += f" AND #wws{idx} = :ews{idx}"

    # ADR-145 I3/I4: a reset or roll this write applies re-grants that quota
    # at the item's count, so it stamps `gc` in the same write — and pins the
    # count it sized the grant at. A shard whose `shard_count` a doubling
    # raised after this image was captured would otherwise reset at the lower
    # count and cover a slot someone else was just granted (design §8 R5).
    # `<=`, not `=`: the pin only forbids a count above the one read.
    # Positional tokens (#634); `#gq*` is disjoint from every other family here.
    for idx, name in enumerate(granted):
        set_parts.append(f"#gq{idx} = :gq{idx}")
        expr_names[f"#gq{idx}"] = bucket_attr(name, BUCKET_FIELD_GC)
        expr_values[f":gq{idx}"] = state.shard_count
    if granted:
        expr_names["#gqsc"] = "shard_count"
        expr_values[":gqpin"] = state.shard_count
        condition += " AND (attribute_not_exists(#gqsc) OR #gqsc <= :gqpin)"

    # An expired `vu` means this pass is the materialisation the fast path is
    # waiting on: stamp the next boundary so it can resume. A `vu` still in the
    # future is left alone — re-stamping it would move the gate the client is
    # already honouring. A boundary at or before `now` means a duration window
    # is waiting for a client to anchor it (ended, or never opened): the gate
    # must stay shut, so the stamp is left exactly as it is.
    if state.vu_ms is not None and state.vu_ms <= now_ms:
        boundary = _item_next_boundary(state, now_ms)
        if boundary is not None and boundary > now_ms:
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
            limits_refilled=refilled,
            windows_rolled=rolled,
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


def _is_quota_limit(limit_name: str, image: dict[str, Any]) -> bool:
    """Is this limit on this item a quota (ADR-137), read off the stored shape?

    The aggregator holds attributes rather than a ``Limit``, so it cannot ask
    :attr:`Limit.is_quota` directly. This is the same key
    ``Limit.from_bucket_state`` reconstructs one by: a zero refill rate paired
    with a reset — a calendar ``reset_schedule`` or, since ADR-139, a duration
    window's ``b_{name}_rsa``. Both halves are required — a zero rate on its own is
    a corrupt item, and granting it a quota's transfer rule would starve a limit
    that does recover; a reset beside a positive rate is the mirror corruption
    that ``Limit.__post_init__`` rejects.

    The reset schedule may be the limit's own ``b_{name}_rsched`` override or
    the item-level ``rsched`` default it inherits, and the reserved
    ``BUCKET_SCHED_NONE`` marker (#541) means "this limit declares none" and so
    blocks the inheritance. Only the presence of a schedule is asked here, never
    its content, so no decode is needed and an undecodable one cannot make this
    raise.
    """
    ra_attr = bucket_attr(limit_name, BUCKET_FIELD_RA)
    if int(image.get(ra_attr, {}).get("N", "0")) != 0:
        return False
    # Since ADR-139 there are **two** spellings of the reset half on an item:
    # the calendar `rsched` below and the duration window's `b_{name}_rsa`.
    # Both must be recognised, because the caller uses this to decide whether
    # a shard coming into existence is filled by transfer or minted a fresh
    # share (#587), and a duration quota misread as a dripping limit is #587
    # reintroduced for exactly the shape ADR-139 adds.
    #
    # Asked **before** the `BUCKET_SCHED_NONE` test, not after it: a duration
    # quota sharing an item with a calendar quota declares no `reset_schedule`
    # of its own, so its `b_{name}_rsched` holds the #541 marker to block the
    # item's calendar default — and that marker says nothing about its window.
    # `rsa` is per-limit only, with no item-level default to inherit.
    if bucket_attr(limit_name, BUCKET_FIELD_RSA) in image:
        return True
    own = image.get(bucket_attr(limit_name, BUCKET_FIELD_RSCHED), {}).get("S")
    if own == BUCKET_SCHED_NONE:
        return False
    if own:
        return True
    return bool(image.get(BUCKET_FIELD_RSCHED, {}).get("S"))


def _quota_reset_rule(
    limit_name: str, image: dict[str, Any]
) -> tuple[tuple[ScheduleEntry, ...], int | None, str | None]:
    """``(reset_sched, reset_after_seconds, error)`` of a quota, off a stream image.

    Resolved exactly as :func:`_parse_bucket_record` resolves it: the limit's
    own ``b_{name}_rsched`` override, the #541 ``BUCKET_SCHED_NONE`` marker
    blocking the item-level ``rsched``, and otherwise that default. A duration
    window's ``b_{name}_rsa`` makes it a session quota — unless a reset
    schedule rides beside it, the corrupt shape the calendar branch keeps
    (:func:`_window_in_force`). It resolves the rule from shard 0's image and
    applies it to every sibling, which is sound because the #468 fan-out
    keeps a limit's reset rule uniform across an entity's shards.
    """
    tz = image.get(BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    own = image.get(bucket_attr(limit_name, BUCKET_FIELD_RSCHED), {}).get("S")
    if own == BUCKET_SCHED_NONE:
        reset_sched: tuple[ScheduleEntry, ...] = ()
        error = None
    else:
        compact = own or image.get(BUCKET_FIELD_RSCHED, {}).get("S")
        reset_sched, error = _decode_reset_schedule(compact, tz)
    rsa_raw = image.get(bucket_attr(limit_name, BUCKET_FIELD_RSA), {}).get("N")
    rsa = int(rsa_raw) if rsa_raw is not None and not reset_sched else None
    return reset_sched, rsa, error


def _stored_count(item: dict[str, Any]) -> int:
    """An item's stored ``shard_count`` (native value); absent or < 1 reads as 1."""
    return max(1, int(item.get("shard_count", 1)))


def _quota_sibling_from_image(
    shard_id: int,
    item: dict[str, Any],
    limit_name: str,
    reset_rule: tuple[tuple[ScheduleEntry, ...], int | None],
    capacity_milli: int,
    now_ms: int,
) -> QuotaSibling | None:
    """One sibling as the ADR-145 planner sees it, off a ``Table`` item.

    The Lambda mirror of ``Repository._quota_sibling``, on the native values
    the boto3 ``Table`` resource returns — the same rules, not a looser
    variant. ``None`` when the shard lacks the quota. A sibling with no ``gc``
    (design §9) reads as granted at its stored count, halved while its balance
    holds more than one share at that count (R7: it was granted at a lower
    one). A corrupt ``gc < 1`` reads as the stored count. Whether its grant
    belongs to the current period is :func:`quota_period_is_current`, the
    single statement of the rule, against the quota's reset rule as shard 0's
    image resolves it.
    """
    tk = item.get(bucket_attr(limit_name, BUCKET_FIELD_TK))
    if tk is None:
        return None
    tokens = int(tk)
    gc = item.get(bucket_attr(limit_name, BUCKET_FIELD_GC))
    if gc is None:
        grant_count = _stored_count(item)
        while grant_count > 1 and tokens > capacity_milli // grant_count:
            grant_count //= 2
    else:
        grant_count = int(gc)
        if grant_count < 1:
            grant_count = _stored_count(item)
    ws = item.get(bucket_attr(limit_name, BUCKET_FIELD_WS))
    wa = item.get(bucket_attr(limit_name, BUCKET_FIELD_WA))
    reset_sched, rsa = reset_rule
    return QuotaSibling(
        shard_id=shard_id,
        tokens_milli=tokens,
        grant_count=grant_count,
        current=quota_period_is_current(
            reset_sched,
            rsa,
            int(item.get("rf", 0)),
            int(ws) if ws is not None else None,
            int(wa) if wa is not None else None,
            now_ms,
        ),
    )


def _donor_debit(
    donor_shard: int,
    donor_grant_count: int,
    tokens_milli: int,
    limit_name: str,
    donor: dict[str, Any],
    reset_rule: tuple[tuple[ScheduleEntry, ...], int | None],
    now_ms: int,
) -> QuotaDonorDebit:
    """The donor side of a planned move, with its current-period guard.

    Mirrors ``Repository._donor_debit``: a session quota is guarded on the
    donor's ``wa`` as read, a calendar quota on ``rf`` at or past the reset
    edge in force; a legacy donor (no ``gc``) is matched on the count it
    actually stores.
    """
    reset_sched, rsa = reset_rule
    guard_rf_ms: int | None = None
    guard_wa_ms: int | None = None
    if rsa is not None:
        wa = donor.get(bucket_attr(limit_name, BUCKET_FIELD_WA))
        guard_wa_ms = int(wa) if wa is not None else None
    else:
        guard_rf_ms = prev_reset_edge(reset_sched, now_ms)
    legacy = bucket_attr(limit_name, BUCKET_FIELD_GC) not in donor
    return QuotaDonorDebit(
        shard_id=donor_shard,
        limit_name=limit_name,
        tokens_milli=tokens_milli,
        grant_count=donor_grant_count,
        guard_rf_ms=guard_rf_ms,
        guard_wa_ms=guard_wa_ms,
        legacy_shard_count=_stored_count(donor) if legacy else None,
    )


def _donor_update_items(
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
    debits: list[QuotaDonorDebit],
) -> list[dict[str, Any]]:
    """The donor ``Update`` items of one clone's transaction, native values.

    The Lambda mirror of ``Repository.build_quota_donor_debits`` — same
    condition, same positional tokens (#634): ``ADD tk -x`` under the donor
    existing, still holding ``x``, still carrying the grant count read (or,
    with no ``gc``, the ``shard_count`` read), and its grant still in the
    current period (calendar ``rf >= edge``; session ``wa`` unchanged).
    Several quotas moving off one donor share one ``Update``: a transaction
    may touch an item once.

    Native values, not ``{"N": ...}``: the call goes through
    ``table.meta.client``, which shares the ``Table`` resource's parameter
    transformer and serializes every attribute value itself — a typed value
    would be wrapped a second time.
    """
    by_shard: dict[int, list[QuotaDonorDebit]] = {}
    for debit in debits:
        by_shard.setdefault(debit.shard_id, []).append(debit)
    items: list[dict[str, Any]] = []
    for shard, group in sorted(by_shard.items()):
        names: dict[str, str] = {"#qsc": "shard_count"}
        values: dict[str, int] = {}
        adds: list[str] = []
        conds: list[str] = ["attribute_exists(PK)"]
        for i, debit in enumerate(group):
            names[f"#qt{i}"] = bucket_attr(debit.limit_name, BUCKET_FIELD_TK)
            names[f"#qg{i}"] = bucket_attr(debit.limit_name, BUCKET_FIELD_GC)
            values[f":qx{i}"] = debit.tokens_milli
            values[f":qn{i}"] = -debit.tokens_milli
            values[f":qg{i}"] = debit.grant_count
            adds.append(f"#qt{i} :qn{i}")
            conds.append(f"#qt{i} >= :qx{i}")
            legacy = f":qg{i}"
            if debit.legacy_shard_count is not None:
                legacy = f":ql{i}"
                values[legacy] = debit.legacy_shard_count
            conds.append(f"(#qg{i} = :qg{i} OR (attribute_not_exists(#qg{i}) AND #qsc = {legacy}))")
            if debit.guard_rf_ms is not None:
                names["#qrf"] = "rf"
                values[f":qe{i}"] = debit.guard_rf_ms
                conds.append(f"#qrf >= :qe{i}")
            if debit.guard_wa_ms is not None:
                names[f"#qw{i}"] = bucket_attr(debit.limit_name, BUCKET_FIELD_WA)
                values[f":qw{i}"] = debit.guard_wa_ms
                conds.append(f"#qw{i} = :qw{i}")
        items.append(
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {
                        "PK": pk_bucket(namespace_id, entity_id, resource, shard),
                        "SK": sk_state(),
                    },
                    "UpdateExpression": "ADD " + ", ".join(adds),
                    "ConditionExpression": " AND ".join(conds),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }
        )
    return items


def _quota_count_freeze(
    key: dict[str, str], new_count: int, quota_grants: list[tuple[str, int]]
) -> dict[str, Any]:
    """Path 1's raise for a shard carrying a legacy quota, with its grant size frozen.

    The Lambda mirror of ``Repository._build_quota_count_freeze``:
    ``SET shard_count = :new, gc = if_not_exists(gc, :g)`` per quota, under
    ``shard_count < :new``. Raising a legacy (no ``gc``) item's count alone
    would shrink the grant the planner read it at to the new count — a slot it
    still covers would be minted again later — and fail the donor debit's
    legacy branch. Native values: this goes through the ``Table`` resource.
    """
    names: dict[str, str] = {"#qsc": "shard_count"}
    sets = ["#qsc = :qnew"]
    values: dict[str, int] = {":qnew": new_count}
    for i, (name, grant_count) in enumerate(quota_grants):
        names[f"#qf{i}"] = bucket_attr(name, BUCKET_FIELD_GC)
        values[f":qo{i}"] = grant_count
        sets.append(f"#qf{i} = if_not_exists(#qf{i}, :qo{i})")
    return {
        "Key": key,
        "UpdateExpression": "SET " + ", ".join(sets),
        "ConditionExpression": "#qsc < :qnew",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def _legacy_quota_sizes(
    shard_id: int, item: dict[str, Any], skip: set[str], now_ms: int
) -> list[tuple[str, int]]:
    """``(name, grant_count)`` for every legacy quota a ``Table`` item carries.

    The quotas shard 0's image does not carry, which Path 1 would otherwise
    raise without freezing: each one on ``item`` with no ``gc`` (design §9),
    sized by the same R7 rule as :func:`_quota_sibling_from_image` against
    its own schedule-effective capacity. The shape test is
    :func:`_is_quota_limit`, on the item re-serialized to the stream's wire
    format. A schedule that cannot be decoded sizes against the base
    capacity: the freeze is then the one the reader would infer without it.
    """
    serializer = TypeSerializer()
    wire = {key: serializer.serialize(value) for key, value in item.items()}
    tz = wire.get(BUCKET_FIELD_SCHED_TZ, {}).get("S", "UTC")
    sizes: list[tuple[str, int]] = []
    for limit_name, info in _extract_limit_attrs(wire).items():
        if (
            limit_name == WCU_LIMIT_NAME
            or limit_name in skip
            or bucket_attr(limit_name, BUCKET_FIELD_GC) in item
            or not _is_quota_limit(limit_name, wire)
        ):
            continue
        own = wire.get(bucket_attr(limit_name, BUCKET_FIELD_SCHED), {}).get("S")
        sched: tuple[ScheduleEntry, ...] = ()
        if own != BUCKET_SCHED_NONE:
            sched, error = _decode_schedule(own or wire.get(BUCKET_FIELD_SCHED, {}).get("S"), tz)
            if error is not None:
                sched = ()
        capacity = effective_params(info["cp_milli"], 0, info["rp_ms"], sched, now_ms)[0]
        sibling = _quota_sibling_from_image(
            shard_id, item, limit_name, ((), None), capacity, now_ms
        )
        if sibling is not None:
            sizes.append((limit_name, sibling.grant_count))
    return sizes


def _repair_quota_clones(
    table: Any,
    namespace_id: str,
    entity_id: str,
    resource: str,
    new_count: int,
    clones: list[int],
    quota_names: list[str],
    seen_count: int,
) -> None:
    """Raise quota clones a later doubling overtook while they were created (ADR-145).

    The clone ``Put`` is guarded only by ``attribute_not_exists(PK)``, not by
    the count it was planned at. A client can double shard 0 after the old
    shards were read; its propagation finds no clone to raise, and the clone
    would then reset at ``new_count`` every period beside a shard created at
    the higher count that covers one of its slots. So, once per record that
    created a quota clone: one strongly consistent ``GetItem`` of shard 0's
    ``shard_count`` (1 RCU), compared with the counts the old shards already
    read; when either is higher, each created clone is raised with its current
    grant frozen at ``new_count`` (:func:`_quota_count_freeze`). Shard 0 is
    never written. A failure — a ``ClientError`` or a ``BotoCoreError`` such
    as a dropped connection — is logged, without the entity id, and swallowed:
    the clones exist and are funded; only the next period's grant is at stake,
    and failing the stream batch would re-drive writes that already landed.
    """
    shard = 0  # the item being read or written, for the log line
    try:
        response = table.get_item(
            Key={"PK": pk_bucket(namespace_id, entity_id, resource, 0), "SK": sk_state()},
            ProjectionExpression="shard_count",
            ConsistentRead=True,
        )
        current = max(seen_count, _stored_count(response.get("Item") or {}))
        if current <= new_count:
            return
        for shard in clones:
            key = {"PK": pk_bucket(namespace_id, entity_id, resource, shard), "SK": sk_state()}
            try:
                table.update_item(
                    **_quota_count_freeze(key, current, [(name, new_count) for name in quota_names])
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
    except ClientError as e:
        logger.warning(
            "Quota clone count repair failed - clones keep their created count",
            resource=resource,
            shard=shard,
            error_code=e.response.get("Error", {}).get("Code"),
        )
    except BotoCoreError as e:
        logger.warning(
            "Quota clone count repair failed - clones keep their created count",
            resource=resource,
            shard=shard,
            error_type=type(e).__name__,
        )


def propagate_shard_count(
    table: Any,
    record: dict[str, Any],
    now_ms: int | None = None,
) -> int:
    """Propagate shard_count changes to all other shard items.

    Detects shard_count change in stream record (OldImage vs NewImage).
    Only propagates from shard 0. Two code paths:
    - Existing shards (1..old_count-1): UpdateItem with shard_count < :new;
      a shard carrying a quota also has the grant size it was read at frozen
      (``gc = if_not_exists(gc, :g)``, ADR-145 R5).
    - New shards (old_count..new_count-1): PutItem cloned from shard 0's
      NewImage with adjusted PK/GSI keys and effective token capacity.
      Uses attribute_not_exists(PK) to avoid overwriting client-created items.
      A quota on the clone is funded by a move off the current-period sibling
      covering its slot — its parent ``target % old_count`` — in one
      ``TransactWriteItems`` with the Put (ADR-145), or granted fresh when no
      sibling covers it. When the item carries a quota, the old shards are
      read once (strongly consistent ``GetItem`` each) before Path 1.

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
    # A quota's is decided per target shard instead (ADR-145), so its share,
    # undivided capacity and reset rule are held apart.
    starting_tokens: dict[str, int] = {}
    quota_shares: dict[str, int] = {}
    quota_capacity: dict[str, int] = {}
    quota_rules: dict[str, tuple[tuple[ScheduleEntry, ...], int | None]] = {}
    clone_error: tuple[str, str] | None = None
    for limit_name, info in limit_attrs.items():
        if limit_name == WCU_LIMIT_NAME:
            starting_tokens[limit_name] = info["cp_milli"]  # per-partition, not divided
            continue
        limit_compact = new_image.get(
            f"{BUCKET_ATTR_PREFIX}{limit_name}_{BUCKET_FIELD_SCHED}", {}
        ).get("S")
        # `BUCKET_SCHED_NONE` means this limit declares no schedule (#541), so
        # it takes neither its own nor the item's: seeded from the item
        # default, an unscheduled limit's new shard would start at half its
        # share for the life of the window.
        declares_none = limit_compact == BUCKET_SCHED_NONE
        limit_sched, limit_error = (
            ((), None) if declares_none else _decode_schedule(limit_compact, sched_tz)
        )
        error = sched_error or limit_error
        if error is None and _is_quota_limit(limit_name, new_image):
            reset_sched, rsa, error = _quota_reset_rule(limit_name, new_image)
            quota_rules[limit_name] = (reset_sched, rsa)
        if error is not None:
            clone_error = (limit_name, error)
            break
        scaled_cp, _ra, _rp = effective_params(
            info["cp_milli"],
            info["ra_milli"],
            info["rp_ms"],
            () if declares_none else (limit_sched or item_sched),
            now_ms,
        )
        share = scaled_cp // new_count
        if limit_name in quota_rules:
            quota_shares[limit_name] = share
            quota_capacity[limit_name] = scaled_cp
        else:
            starting_tokens[limit_name] = share

    # ADR-145: a quota's clones are funded from the shards that already exist,
    # read once per doubling — strongly consistent, because a donor debit
    # sized off a stale balance only fails its condition and skips the clone.
    # Read *before* Path 1 raises their counts: a legacy (no `gc`) sibling's
    # grant size is inferred from the count it stores (R7), exactly as the
    # client's planner reads it, and Path 1 then freezes that size onto it.
    #
    # A failed read must not cost the propagation itself: Path 1 still raises
    # every lagging shard (without the grant-size freeze, which needs what the
    # read would have said — the one-period legacy residual of design §9), and
    # no quota clone is pre-created, since its funding cannot be sized. Every
    # clone of this item carries the quota, so that is every clone; the client
    # creates them lazily.
    old_items: dict[int, dict[str, Any]] = {}
    siblings: dict[str, list[QuotaSibling]] = {}
    extra_frozen: dict[int, list[tuple[str, int]]] = {}
    read_failed = False
    if quota_shares and clone_error is None:
        try:
            for shard in range(old_count):
                response = table.get_item(
                    Key={
                        "PK": pk_bucket(namespace_id, entity_id, resource, shard),
                        "SK": sk_state(),
                    },
                    ConsistentRead=True,
                )
                if "Item" in response:
                    old_items[shard] = response["Item"]
        except ClientError as e:
            # Never the entity id: it is routinely an API key.
            logger.warning(
                "Sibling read failed - propagating without the quota freeze or clones",
                resource=resource,
                error_code=e.response.get("Error", {}).get("Code"),
            )
            old_items = {}
            read_failed = True
    if quota_shares and clone_error is None and not read_failed:
        # Every quota each sibling carries is frozen when Path 1 raises it,
        # not only the ones shard 0's image carries: a legacy quota left
        # unfrozen would read as granted at the raised count (design §9).
        for shard, item in old_items.items():
            legacy = _legacy_quota_sizes(shard, item, set(quota_shares), now_ms)
            if legacy:
                extra_frozen[shard] = legacy
        for limit_name in quota_shares:
            siblings[limit_name] = [
                sibling
                for sibling in (
                    _quota_sibling_from_image(
                        shard,
                        item,
                        limit_name,
                        quota_rules[limit_name],
                        quota_capacity[limit_name],
                        now_ms,
                    )
                    for shard, item in sorted(old_items.items())
                )
                if sibling is not None
            ]

    # Path 1: Update existing shards (lightweight shard_count update). A shard
    # carrying a quota also has the grant size the planner read it at frozen
    # onto it (`gc = if_not_exists(gc, :g)`), mirroring the client's R5 raise.
    for target_shard in range(1, old_count):
        key = {"PK": pk_bucket(namespace_id, entity_id, resource, target_shard), "SK": sk_state()}
        frozen = [
            (name, sibling.grant_count)
            for name, group in siblings.items()
            for sibling in group
            if sibling.shard_id == target_shard
        ] + extra_frozen.get(target_shard, [])
        try:
            if frozen:
                table.update_item(**_quota_count_freeze(key, new_count, frozen))
            else:
                table.update_item(
                    Key=key,
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

    if clone_error is not None:
        logger.warning(
            "Undecodable stored schedule - not pre-creating shards",
            entity_id=entity_id,
            resource=resource,
            limit_name=clone_error[0],
            reason=clone_error[1],
        )
        return updated
    if read_failed:
        return updated

    # Path 2: Pre-create new shards (full item cloned from shard 0)
    # A quota clone is planned at this record's `new_count`, which the client
    # would not do: it plans at the largest count any sibling stores. With the
    # stream lagging across two doublings, a clone sized at the older count
    # covers slots a client may already have granted at the newer one — paid
    # twice. So a record overtaken by a later doubling pre-creates no quota
    # clone; a later record, or the client, creates them at the right count.
    stored_counts = [_stored_count(item) for item in old_items.values()]
    if quota_shares and any(count > new_count for count in stored_counts):
        logger.debug(
            "Quota clones skipped - the record is overtaken by a later doubling",
            resource=resource,
            record_count=new_count,
            stored_count=max(stored_counts),
        )
        return updated
    client = table.meta.client
    # The quotas whose grant on shard 0's image is not current — a reset or
    # roll pending there, which every clone of that image inherits (R13).
    image_rf = int(new_image.get("rf", {}).get("N", "0"))
    stale_on_image: set[str] = set()
    for limit_name, (reset_sched, rsa) in quota_rules.items():
        ws_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_WS), {}).get("N")
        wa_raw = new_image.get(bucket_attr(limit_name, BUCKET_FIELD_WA), {}).get("N")
        if not quota_period_is_current(
            reset_sched,
            rsa,
            image_rf,
            int(ws_raw) if ws_raw is not None else None,
            int(wa_raw) if wa_raw is not None else None,
            now_ms,
        ):
            stale_on_image.add(limit_name)
    created_clones: list[int] = []
    for target_shard in range(old_count, new_count):
        item = dict(base_item)
        item["PK"] = pk_bucket(namespace_id, entity_id, resource, target_shard)
        item["GSI2SK"] = gsi2_sk_bucket(entity_id, target_shard)
        item["GSI3SK"] = gsi3_sk_bucket(resource, target_shard)
        item["GSI4SK"] = gsi4_sk_bucket(entity_id, resource, target_shard)
        item["shard_count"] = new_count
        # A quota never drips (ADR-137), so a clone is funded by a **move**,
        # never a mint of a slot someone was already granted (ADR-145): the
        # shared planner picks the current-period sibling covering this slot
        # — its parent `target % old_count` whenever the parent's grant covers
        # it — and the clone takes `min(share, donor tk)` off it in one
        # transaction with its own Put. No covering sibling means nobody was
        # granted this slot this period, and it gets a fresh share.
        grants: dict[str, int] = dict(starting_tokens)
        debits: list[QuotaDonorDebit] = []
        stale_move = False
        for limit_name, share in quota_shares.items():
            grant = plan_quota_grant(siblings[limit_name], target_shard, new_count, share)
            grants[limit_name] = grant.tokens_milli
            item[bucket_attr(limit_name, BUCKET_FIELD_GC)] = new_count
            if grant.donor_shard is not None and limit_name in stale_on_image:
                stale_move = True
            if (
                grant.donor_shard is not None
                and grant.donor_grant_count is not None
                and grant.tokens_milli > 0
            ):
                debits.append(
                    _donor_debit(
                        grant.donor_shard,
                        grant.donor_grant_count,
                        grant.tokens_milli,
                        limit_name,
                        old_items[grant.donor_shard],
                        quota_rules[limit_name],
                        now_ms,
                    )
                )
        if stale_move:
            # R13: the clone copies shard 0's `rf`/`ws`/`wa`, so it would
            # inherit shard 0's pending reset or roll for this quota. Its next
            # pass would then SET a fresh share over what it was moved,
            # while the donor's grant still covers the slot — that slot paid
            # twice. Pre-create nothing (no Put, no debit): the client creates
            # the shard lazily and stamps its own period.
            logger.debug(
                "Clone skipped - a quota move onto a pending reset or roll",
                resource=resource,
                shard_id=target_shard,
            )
            continue
        for limit_name, tokens in grants.items():
            item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = tokens
            item[bucket_attr(limit_name, BUCKET_FIELD_TC)] = 0
            # A clone of a shard with a pending roll inherits that roll
            # (`ws`/`wa` are copied verbatim). Its consumption counter
            # restarts at 0, so its snapshot does too (#640): everything
            # the clone spends before the roll is charged to that window.
            wtc_attr = bucket_attr(limit_name, BUCKET_FIELD_WTC)
            if wtc_attr in item:
                item[wtc_attr] = 0
        try:
            if debits:
                # The boto3 `Table` resource has no transaction call, so this
                # goes through `table.meta.client`, which shares the resource's
                # serializer: native values, exactly like `put_item` below.
                # Put and Update inside a transaction are authorised by
                # PutItem / UpdateItem, which the aggregator role already has
                # (design §6) — no ConditionCheck.
                client.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": table.name,
                                "Item": item,
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                        *_donor_update_items(table.name, namespace_id, entity_id, resource, debits),
                    ]
                )
            else:
                table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(PK)",
                )
            updated += 1
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
                # The client already created this shard, or a donor moved
                # since the read. Either way the whole write is undone (the
                # donor untouched) and the client creates the shard lazily.
                if code == "TransactionCanceledException":
                    logger.debug(
                        "Clone transaction cancelled - left to the client",
                        resource=resource,
                        shard_id=target_shard,
                        reasons=[
                            reason.get("Code", "None")
                            for reason in e.response.get("CancellationReasons", [])
                        ],
                    )
                continue
            raise
        if quota_shares:
            created_clones.append(target_shard)
        # A later clone planning off the same donor sees what it has left.
        for debit in debits:
            siblings[debit.limit_name] = [
                replace(s, tokens_milli=s.tokens_milli - debit.tokens_milli)
                if s.shard_id == debit.shard_id
                else s
                for s in siblings[debit.limit_name]
            ]

    if created_clones:
        _repair_quota_clones(
            table,
            namespace_id,
            entity_id,
            resource,
            new_count,
            created_clones,
            list(quota_shares),
            max(stored_counts, default=new_count),
        )

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
