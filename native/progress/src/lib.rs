//! Pure history reduction. Python owns SQL selection, snapshots, authorization and writes.
//! Only a bounded chunk crosses the Python boundary; no paths, database handles or credentials.
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

const CHUNK: usize = 256;
type Evidence = (Vec<i64>, Vec<(f64, f64)>, bool);
type Checkpoint = (
    String,
    String,
    Vec<i64>,
    Vec<(f64, f64)>,
    bool,
    u64,
    Option<f64>,
);

#[derive(Clone)]
struct Item {
    kind: String,
    pages: i64,
    duration: f64,
}

struct Row {
    email: String,
    item: String,
    kind: String,
    ts: Py<PyAny>,
    payload: String,
}

#[derive(Default)]
struct State {
    pages: HashSet<i64>,
    ranges: Vec<(f64, f64)>,
    opened: bool,
    attempts: u64,
    last: Option<Timestamp>,
}

enum Timestamp {
    Number(f64),
    Legacy(Py<PyAny>),
}

fn numeric_last(previous: Option<f64>, current: f64) -> f64 {
    match previous {
        Some(previous) if previous != 0.0 => previous.max(current),
        _ => current,
    }
}

fn activity_time(
    py: Python<'_>,
    state: &mut State,
    row: &Row,
    maximum: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let timestamp = row.ts.bind(py);
    if timestamp.is_instance_of::<PyFloat>() {
        let number = timestamp.extract::<f64>()?;
        if number.is_finite() {
            match state.last {
                None => {
                    state.last = Some(Timestamp::Number(number));
                    return Ok(());
                }
                Some(Timestamp::Number(previous)) if previous.is_finite() => {
                    state.last = Some(Timestamp::Number(numeric_last(Some(previous), number)));
                    return Ok(());
                }
                _ => {}
            }
        }
    }
    // Recovery historically permits nonnumeric timestamps. Preserve Python's
    // comparison (or its exception), and never inspect timestamps of non-evidence.
    let previous = match &state.last {
        Some(Timestamp::Number(n)) => n.into_pyobject(py)?.into_any(),
        Some(Timestamp::Legacy(value)) => value.bind(py).clone(),
        None => py.None().into_bound(py),
    };
    let previous = if previous.is_truthy()? {
        previous
    } else {
        timestamp.clone()
    };
    state.last = Some(Timestamp::Legacy(
        maximum.call1((previous, timestamp))?.unbind(),
    ));
    Ok(())
}

fn intervals(ranges: &[(f64, f64)], duration: f64) -> Vec<(f64, f64)> {
    let mut valid: Vec<_> = ranges
        .iter()
        .copied()
        .filter(|&(a, b)| {
            a.is_finite() && b.is_finite() && 0.0 <= a && a < b && b <= duration + 0.5
        })
        .map(|(a, b)| (a.max(0.0), b.min(duration)))
        .collect();
    valid.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
    let mut merged: Vec<(f64, f64)> = Vec::new();
    for (start, end) in valid {
        if let Some(last) = merged.last_mut() {
            if start <= last.1 {
                last.1 = last.1.max(end);
                continue;
            }
        }
        merged.push((start, end));
    }
    merged
}

// Decode evidence without retaining unrelated audit metadata. Unlike
// IgnoredAny, walk nested values through deserialize_any so malformed values
// and the JSON recursion limit still take the Python compatibility path.
struct Discard;
impl<'de> serde::Deserialize<'de> for Discard {
    fn deserialize<D: serde::Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        decoder.deserialize_any(Discard)
    }
}
impl<'de> serde::de::Visitor<'de> for Discard {
    type Value = Discard;
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
        Ok(self)
    }
    fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        while seq.next_element::<Discard>()?.is_some() {}
        Ok(self)
    }
    fn visit_map<A: serde::de::MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        while map.next_entry::<Discard, Discard>()?.is_some() {}
        Ok(self)
    }
}
struct Payload {
    detail: Value,
}
impl<'de> serde::Deserialize<'de> for Payload {
    fn deserialize<D: serde::Deserializer<'de>>(decoder: D) -> Result<Self, D::Error> {
        struct Object;
        impl<'de> serde::de::Visitor<'de> for Object {
            type Value = Payload;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("an audit object")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut map: A,
            ) -> Result<Payload, A::Error> {
                let mut detail = Value::Object(serde_json::Map::new());
                while let Some(key) = map.next_key::<String>()? {
                    if key == "detail" {
                        detail = map.next_value()?;
                    } else {
                        map.next_value::<Discard>()?;
                    }
                }
                Ok(Payload { detail })
            }
        }
        decoder.deserialize_map(Object)
    }
}
fn parse_payload(text: &str) -> Option<Payload> {
    serde_json::from_str(text).ok()
}

// None means use the Python reference parser for unusual historical JSON, preserving
// NaN handling, arbitrary integers, malformed-data exceptions and ignored extra fields.
fn evidence(row: &Row, item: &Item) -> Option<Evidence> {
    if row.kind == "view_requested" {
        return Some((vec![], vec![], false));
    }
    let object = parse_payload(&row.payload)?;
    if row.kind == "item_open" {
        return Some((vec![], vec![], true));
    }
    let detail = object.detail.as_object()?;
    if row.kind == "video_progress" && item.kind == "video" {
        let mut pairs = vec![];
        if let Some(value) = detail.get("ranges") {
            for pair in value.as_array()? {
                let Some(pair) = pair.as_array().filter(|p| p.len() == 2) else {
                    continue;
                };
                if !pair.iter().all(Value::is_number) {
                    continue;
                }
                let a = pair[0].as_f64()?;
                let b = pair[1].as_f64()?;
                // Rare huge integers use Python's exact conversion/refusal behavior.
                if a.abs() > (1_u64 << 53) as f64 || b.abs() > (1_u64 << 53) as f64 {
                    return None;
                }
                pairs.push((a, b));
            }
        }
        let ranges = intervals(&pairs, item.duration);
        let recorded = !ranges.is_empty();
        return Some((vec![], ranges, recorded));
    }
    if (row.kind == "page_served" || row.kind == "item_summary") && item.kind != "video" {
        let single;
        let pages = if row.kind == "page_served" {
            single = vec![detail.get("page").cloned().unwrap_or(Value::Null)];
            &single
        } else {
            match detail.get("pages") {
                Some(v) => v.as_array()?,
                None => return Some((vec![], vec![], false)),
            }
        };
        if pages
            .iter()
            .any(|v| v.as_f64().is_some_and(|n| n == 0.0 && n.is_sign_negative()))
        {
            // JSON -0 is an int in Python, while -0.0 is a float: retain that distinction.
            return None;
        }
        let total = if item.kind == "photo" { 1 } else { item.pages };
        let valid: Vec<_> = pages
            .iter()
            .filter_map(Value::as_i64)
            .filter(|&p| 0 <= p && p < total)
            .collect();
        let recorded = !valid.is_empty() && row.kind == "page_served";
        return Some((valid, vec![], recorded));
    }
    Some((vec![], vec![], false))
}

fn apply(state: &mut State, row: &Row, data: Evidence) -> PyResult<()> {
    if row.kind == "view_requested" {
        state.attempts = state
            .attempts
            .checked_add(1)
            .ok_or_else(|| pyo3::exceptions::PyOverflowError::new_err("attempt count overflow"))?;
        return Ok(());
    }
    if row.kind == "item_open" {
        state.opened = true;
    }
    state.pages.extend(data.0);
    state.ranges.extend(data.1);
    Ok(())
}

fn portable_clock(clock: &Bound<'_, PyAny>) -> PyResult<f64> {
    let invalid =
        || pyo3::exceptions::PyValueError::new_err("incremental replay requires portable clocks");
    if clock.is_instance_of::<PyBool>() {
        return Err(invalid());
    }
    let number = if clock.is_instance_of::<PyInt>() {
        // Check the exact integer before float conversion can round2**53+1 down.
        let integer = clock.extract::<i64>()?;
        if integer <= 0 || integer > (1_i64 << 53) {
            return Err(invalid());
        }
        integer as f64
    } else if clock.is_instance_of::<PyFloat>() {
        clock.extract::<f64>()?
    } else {
        return Err(invalid());
    };
    if !number.is_finite() || number <= 0.0 || number > (1_u64 << 53) as f64 {
        return Err(invalid());
    }
    Ok(number)
}

fn reduce_inner<'py>(
    py: Python<'py>,
    metadata: Vec<(String, String, i64, f64)>,
    rows: &Bound<'py, PyAny>,
    compatibility: &Bound<'py, PyAny>,
    checkpoint: Option<Vec<Checkpoint>>,
) -> PyResult<(Bound<'py, PyDict>, Vec<Checkpoint>)> {
    let items: HashMap<_, _> = metadata
        .into_iter()
        .map(|(id, kind, pages, duration)| {
            (
                id,
                Item {
                    kind,
                    pages,
                    duration,
                },
            )
        })
        .collect();
    let mut iterator = rows.try_iter()?;
    let mut states: HashMap<(String, String), State> = HashMap::new();
    let incremental = checkpoint.is_some();
    if let Some(checkpoint) = checkpoint {
        for (email, id, pages, ranges, opened, attempts, last) in checkpoint {
            let item = items.get(&id).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err("checkpoint item absent from metadata")
            })?;
            if last.is_some_and(|n| !n.is_finite() || n <= 0.0 || n > (1_u64 << 53) as f64)
                || intervals(&ranges, item.duration) != ranges
                || (item.kind != "video" && !ranges.is_empty())
                || pages.iter().any(|p| {
                    item.kind == "video"
                        || *p < 0
                        || *p >= if item.kind == "photo" { 1 } else { item.pages }
                })
            {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "noncanonical checkpoint",
                ));
            }
            let state = State {
                pages: pages.into_iter().collect(),
                ranges,
                opened,
                attempts,
                last: last.map(Timestamp::Number),
            };
            if states.insert((email, id), state).is_some() {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "duplicate checkpoint key",
                ));
            }
        }
    }
    let maximum = py.import("builtins")?.getattr("max")?;
    loop {
        let mut chunk = Vec::with_capacity(CHUNK);
        let mut exhausted = false;
        for _ in 0..CHUNK {
            let Some(row) = iterator.next() else {
                exhausted = true;
                break;
            };
            let row = row?;
            let identity: Option<String> = row.get_item(1)?.extract()?;
            let Some(identity) = identity.filter(|id| items.contains_key(id)) else {
                continue;
            };
            let kind: String = row.get_item(2)?.extract()?;
            let payload = if kind == "view_requested" {
                String::new()
            } else {
                row.get_item(4)?.extract()?
            };
            chunk.push(Row {
                email: row.get_item(0)?.extract()?,
                item: identity,
                kind,
                ts: row.get_item(3)?.unbind(),
                payload,
            });
        }
        // An entire batch may contain irrelevant items; that is not end-of-cursor.
        if chunk.is_empty() && exhausted {
            break;
        }
        let mut parsed = py.detach(|| {
            chunk
                .iter()
                .map(|row| evidence(row, &items[&row.item]))
                .collect::<Vec<_>>()
        });
        for (row, data) in chunk.iter().zip(parsed.iter_mut()) {
            if data.is_none() {
                let item = &items[&row.item];
                let fallback = compatibility.call1((
                    &row.kind,
                    &row.payload,
                    &item.kind,
                    item.pages,
                    item.duration,
                ))?;
                let mut ranges = vec![];
                for pair in fallback.get_item(1)?.try_iter()? {
                    let pair = pair?;
                    ranges.push((pair.get_item(0)?.extract()?, pair.get_item(1)?.extract()?));
                }
                *data = Some((
                    fallback.get_item(0)?.extract()?,
                    ranges,
                    fallback.get_item(2)?.extract()?,
                ));
            }
        }
        for (row, data) in chunk.into_iter().zip(parsed) {
            if incremental {
                portable_clock(row.ts.bind(py))?;
            }
            let state = states
                .entry((row.email.clone(), row.item.clone()))
                .or_default();
            let data = data.expect("compatibility filled missing evidence");
            if data.2 {
                activity_time(py, state, &row, &maximum)?;
            }
            apply(state, &row, data)?;
        }
        py.check_signals()?;
        if exhausted {
            break;
        }
    }
    let finished = py.detach(|| {
        states
            .into_iter()
            .map(|((email, id), state)| {
                let ranges = intervals(&state.ranges, items[&id].duration);
                (email, id, state, ranges)
            })
            .collect::<Vec<_>>()
    });
    let result = PyDict::new(py);
    let mut checkpoint = Vec::new();
    let sum = py.import("builtins")?.getattr("sum")?;
    for (email, id, state, ranges) in finished {
        if incremental {
            let last = match &state.last {
                Some(Timestamp::Number(n)) => Some(*n),
                Some(Timestamp::Legacy(value)) => Some(value.bind(py).extract::<f64>()?),
                None => None,
            };
            checkpoint.push((
                email.clone(),
                id.clone(),
                state.pages.iter().copied().collect(),
                ranges.clone(),
                state.opened,
                state.attempts,
                last,
            ));
        }
        let value = PyDict::new(py);
        value.set_item("pages", PySet::new(py, state.pages)?)?;
        value.set_item("opened", state.opened)?;
        value.set_item("attempts", state.attempts)?;
        match state.last {
            Some(Timestamp::Number(n)) => value.set_item("last_activity_at", n)?,
            Some(Timestamp::Legacy(timestamp)) => value.set_item("last_activity_at", timestamp)?,
            None => value.set_item("last_activity_at", py.None())?,
        }
        // Python's compensated float sum is part of the existing completion contract.
        let lengths = PyList::new(py, ranges.into_iter().map(|(a, b)| b - a))?;
        value.set_item("watched_seconds", sum.call1((lengths,))?)?;
        result.set_item((email, id), value)?;
    }
    Ok((result, checkpoint))
}

#[pyfunction]
fn reduce<'py>(
    py: Python<'py>,
    metadata: Vec<(String, String, i64, f64)>,
    rows: &Bound<'py, PyAny>,
    compatibility: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    Ok(reduce_inner(py, metadata, rows, compatibility, None)?.0)
}

#[pyfunction]
fn reduce_incremental<'py>(
    py: Python<'py>,
    metadata: Vec<(String, String, i64, f64)>,
    rows: &Bound<'py, PyAny>,
    compatibility: &Bound<'py, PyAny>,
    checkpoint: &Bound<'py, PyList>,
) -> PyResult<(Bound<'py, PyDict>, Vec<Checkpoint>)> {
    // Validate before Vec<f64> extraction can coerce a nonportable integer.
    for entry in checkpoint.iter() {
        let clock = entry.get_item(6)?;
        if !clock.is_none() {
            portable_clock(&clock)?;
        }
    }
    reduce_inner(
        py,
        metadata,
        rows,
        compatibility,
        Some(checkpoint.extract()?),
    )
}

#[pymodule]
fn postern_progress_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("API_VERSION", 1)?;
    m.add_function(wrap_pyfunction!(reduce, m)?)?;
    m.add("CHECKPOINT_VERSION", 1)?;
    m.add_function(wrap_pyfunction!(reduce_incremental, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selective_payload_keeps_full_json_validation_and_last_duplicate_detail() {
        let nested = format!(
            r#"{{"detail":{{"page":1}},"unused":{}0{}}}"#,
            "[".repeat(130),
            "]".repeat(130)
        );
        let cases = [
            "{}",
            r#"{"detail":null}"#,
            "null",
            "[]",
            "[{}]",
            r#"{"detail":{"page":1},"detail":{"page":2}}"#,
            r#"{"det\u0061il":{"page":2},"unused":[true,false,null,3,1.5,{"x":"y"}]}"#,
            r#"{"detail":{"page":1},"unused":"\ud800"}"#,
            r#"{"detail":{"page":1},"unused":1e999}"#,
            r#"{"detail":{"page":1},"unused":[}"#,
            r#"{"detail":{"page":1}} trailing"#,
            &nested,
        ];
        for text in cases {
            // The previous full-Value decoder is an independent oracle for the
            // fast parser's success/fallback boundary, including ignored fields.
            let reference = serde_json::from_str::<Value>(text).ok().and_then(|value| {
                value.as_object().map(|object| {
                    object
                        .get("detail")
                        .cloned()
                        .unwrap_or_else(|| Value::Object(serde_json::Map::new()))
                })
            });
            assert_eq!(
                parse_payload(text).map(|value| value.detail),
                reference,
                "{text}"
            );
        }
    }

    #[test]
    fn interval_union_keeps_the_existing_half_second_tolerance() {
        assert_eq!(
            intervals(
                &[
                    (20., 40.),
                    (0., 30.),
                    (40., 90.),
                    (99., 100.5),
                    (0., 100.6),
                    (-1., 90.),
                    (f64::NAN, 100.)
                ],
                100.
            ),
            vec![(0., 90.), (99., 100.)]
        );
    }

    #[test]
    fn clamped_evidence_is_revalidated_at_final_reduction() {
        let first = intervals(&[(100.1, 100.2)], 100.);
        assert_eq!(first, vec![(100.1, 100.)]);
        assert!(intervals(&first, 100.).is_empty());
    }

    #[test]
    fn zero_timestamp_matches_python_truthiness() {
        assert_eq!(numeric_last(Some(0.), -5.), -5.);
        assert_eq!(numeric_last(Some(2.), 1.), 2.);
    }
}
