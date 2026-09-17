"""Schema cache and datatype resolution helper for SQL validation."""

import time

# In-memory schema cache with timestamp: key -> (schema_dict, expire_timestamp)
_SCHEMA_CACHE: dict[str, tuple[dict[str, str], float]] = {}


def parse_dataset_map(raw_map: str | None) -> dict[str, str]:
    """Parse comma-separated KEY=VALUE pairs into mapping dict."""
    out: dict[str, str] = {}
    if not raw_map:
        return out
    for pair in raw_map.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    return out


def get_cached_schema(table_identifier: str) -> dict[str, str] | None:
    """Retrieve table column datatypes from cache if valid."""
    now = time.time()
    if table_identifier in _SCHEMA_CACHE:
        schema, expires_at = _SCHEMA_CACHE[table_identifier]
        if now < expires_at:
            return schema
        del _SCHEMA_CACHE[table_identifier]
    return None


def store_cached_schema(
    table_identifier: str, schema: dict[str, str], ttl_seconds: int = 300
) -> None:
    """Store table schema in in-memory cache with TTL."""
    _SCHEMA_CACHE[table_identifier] = (schema, time.time() + ttl_seconds)


def normalize_datatype(col_type: str) -> str:
    """Normalize BigQuery / SQL datatype to standard category."""
    t = col_type.upper().strip()
    if "INT" in t or "NUMERIC" in t or "FLOAT" in t or "DOUBLE" in t or "DECIMAL" in t:
        return "NUMERIC"
    if "BOOL" in t:
        return "BOOLEAN"
    if "DATE" in t or "TIME" in t or "TIMESTAMP" in t:
        return "TEMPORAL"
    return "STRING"
