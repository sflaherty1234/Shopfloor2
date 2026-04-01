from flask import Flask, jsonify, request, render_template, send_file, Response
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime
from enum import Enum
import uuid
import threading
import csv
import io
import os

app = Flask(__name__)

# ─── Data Models ─────────────────────────────────────────────────────────────

class WorkOrderStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


@dataclass
class TimeEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    operator_id: str = ""
    operator_name: str = ""
    start_time: str = field(default_factory=lambda: datetime.now().isoformat())
    end_time: Optional[str] = None
    pause_reason: Optional[str] = None


@dataclass
class WorkOrder:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    style: str = ""
    size: str = ""
    colorway: str = ""
    line: str = ""
    target_quantity: int = 0
    target_minutes_per_unit: float = 0.0
    completed_quantity: int = 0
    total_active_mins: float = 0.0
    status: str = WorkOrderStatus.PENDING
    time_entries: list = field(default_factory=list)


@dataclass
class Operator:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    line: str = ""
    role: str = "operator"  # "operator" or "manager"
    pin: str = ""           # 4-digit numeric PIN, empty = no PIN set


# ─── In-Memory State ──────────────────────────────────────────────────────────

state_lock = threading.Lock()
operators: list[Operator] = []
work_orders: list[WorkOrder] = []
require_pin: bool = False

PAUSE_REASONS = ["Bathroom", "Machine Down", "Pattern Issue", "Other"]

# ─── Business Logic ───────────────────────────────────────────────────────────

def _minutes_between(start_iso: str, end_iso: Optional[str]) -> float:
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso) if end_iso else datetime.now()
    return max(0.0, (end - start).total_seconds() / 60.0)


def _update_wo_active_mins(wo: WorkOrder) -> None:
    """Recalculate and store the total active (non-paused) minutes for a work order."""
    total = sum(
        _minutes_between(e["start_time"], e["end_time"])
        for e in wo.time_entries
        if e.get("pause_reason") is None
    )
    wo.total_active_mins = round(total, 1)


def _wo_to_dict(wo: WorkOrder) -> dict:
    _update_wo_active_mins(wo)
    return {
        "id": wo.id,
        "name": wo.name,
        "style": wo.style,
        "size": wo.size,
        "colorway": wo.colorway,
        "line": wo.line,
        "target_quantity": wo.target_quantity,
        "target_minutes_per_unit": wo.target_minutes_per_unit,
        "completed_quantity": wo.completed_quantity,
        "total_active_mins": wo.total_active_mins,
        "status": wo.status,
        "time_entries": wo.time_entries,
    }


def _op_to_dict(op: Operator) -> dict:
    return {"id": op.id, "name": op.name, "line": op.line, "role": op.role,
            "has_pin": bool(op.pin)}


def get_busy_wo_id(operator_id: str) -> Optional[str]:
    for wo in work_orders:
        for e in wo.time_entries:
            if e["operator_id"] == operator_id and e["end_time"] is None:
                return wo.id
    return None


def get_operator_status(operator_id: str) -> str:
    for wo in work_orders:
        for e in wo.time_entries:
            if e["operator_id"] == operator_id and e["end_time"] is None:
                if e.get("pause_reason"):
                    return f"Paused ({e['pause_reason']})"
                else:
                    return f"Working: {wo.name}"
    return "Idle"


def compute_summaries() -> dict:
    now = datetime.now().isoformat()

    style_time: dict[str, float] = {}
    style_count: dict[str, int] = {}
    style_target: dict[str, float] = {}
    pause_totals: dict[str, float] = {}
    op_active: dict[str, float] = {}
    op_pauses: dict[str, dict[str, float]] = {}
    op_units: dict[str, float] = {}

    for wo in work_orders:
        wo_active = 0.0
        for e in wo.time_entries:
            mins = _minutes_between(e["start_time"], e["end_time"])
            oid = e["operator_id"]
            if e.get("pause_reason") is None:
                wo_active += mins
                op_active[oid] = op_active.get(oid, 0.0) + mins
            else:
                reason = e["pause_reason"]
                pause_totals[reason] = pause_totals.get(reason, 0.0) + mins
                if oid not in op_pauses:
                    op_pauses[oid] = {}
                op_pauses[oid][reason] = op_pauses[oid].get(reason, 0.0) + mins

        if wo.completed_quantity > 0:
            style_time[wo.style] = style_time.get(wo.style, 0.0) + wo_active
            style_count[wo.style] = style_count.get(wo.style, 0) + wo.completed_quantity
            style_target[wo.style] = wo.target_minutes_per_unit

            if wo_active > 0:
                for e in wo.time_entries:
                    if e.get("pause_reason") is None:
                        mins = _minutes_between(e["start_time"], e["end_time"])
                        share = mins / wo_active
                        units = share * wo.completed_quantity
                        oid = e["operator_id"]
                        op_units[oid] = op_units.get(oid, 0.0) + units

    # Mins per unit by style
    actual_mins_per_unit = {
        style: round(style_time[style] / max(style_count.get(style, 1), 1), 2)
        for style in style_time
    }

    # Efficiency % by style
    efficiency_pct = {}
    for style, actual in actual_mins_per_unit.items():
        target = style_target.get(style, 1.0)
        efficiency_pct[style] = round((target / actual) * 100, 1) if actual > 0 else 0.0

    # Operator summaries
    op_summaries = {}
    all_op_ids = set(op_active.keys()) | set(op_pauses.keys())
    for oid in all_op_ids:
        op = next((o for o in operators if o.id == oid), None)
        name = op.name if op else oid
        op_summaries[oid] = {
            "name": name,
            "active_mins": round(op_active.get(oid, 0.0), 1),
            "paused_by_reason": {k: round(v, 1) for k, v in op_pauses.get(oid, {}).items()},
            "status": get_operator_status(oid),
        }

    # Efficiency % by line — weighted across all WOs on that line
    line_target_mins: dict[str, float] = {}  # sum(target_mins_per_unit * completed_qty)
    line_actual_mins: dict[str, float] = {}  # sum(total_active_mins)
    for wo in work_orders:
        if wo.completed_quantity > 0 and wo.total_active_mins > 0:
            line = wo.line or "Unknown"
            line_target_mins[line] = line_target_mins.get(line, 0.0) + (wo.target_minutes_per_unit * wo.completed_quantity)
            line_actual_mins[line] = line_actual_mins.get(line, 0.0) + wo.total_active_mins
    line_efficiency: dict[str, float] = {}
    for line in line_actual_mins:
        if line_actual_mins[line] > 0:
            line_efficiency[line] = round((line_target_mins[line] / line_actual_mins[line]) * 100, 1)

    return {
        "style_mins_per_unit": actual_mins_per_unit,
        "efficiency_pct": efficiency_pct,
        "pause_totals": {k: round(v, 1) for k, v in pause_totals.items()},
        "operator_summaries": op_summaries,
        "line_efficiency": line_efficiency,
    }


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def get_state():
    with state_lock:
        return jsonify({
            "operators": [_op_to_dict(o) for o in operators],
            "work_orders": [_wo_to_dict(wo) for wo in work_orders],
            "summaries": compute_summaries(),
            "pause_reasons": PAUSE_REASONS,
            "require_pin": require_pin,
        })


@app.route("/api/operators", methods=["POST"])
def add_operator():
    data = request.json
    with state_lock:
        raw_role = data.get("role", "operator").strip().lower()
        role = raw_role if raw_role in ("operator", "manager") else "operator"
        raw_pin = str(data.get("pin", "")).strip()
        pin = raw_pin if raw_pin.isdigit() and len(raw_pin) == 4 else ""
        op = Operator(name=data.get("name", ""), line=data.get("line", ""), role=role, pin=pin)
        operators.append(op)
    return jsonify(_op_to_dict(op)), 201


@app.route("/api/work-orders", methods=["POST"])
def add_work_order():
    data = request.json
    with state_lock:
        wo = WorkOrder(
            name=data.get("name", ""),
            style=data.get("style", ""),
            size=data.get("size", ""),
            colorway=data.get("colorway", ""),
            line=data.get("line", ""),
            target_quantity=int(data.get("target_quantity", 0)),
            target_minutes_per_unit=float(data.get("target_minutes_per_unit", 0.0)),
        )
        work_orders.append(wo)
    return jsonify(_wo_to_dict(wo)), 201


@app.route("/api/work-orders/<wo_id>/start", methods=["POST"])
def start_work(wo_id):
    data = request.json
    operator_id = data.get("operator_id")
    operator_name = data.get("operator_name")
    now = datetime.now().isoformat()

    with state_lock:
        busy_id = get_busy_wo_id(operator_id)
        if busy_id and busy_id != wo_id:
            return jsonify({"error": "Operator already busy on another work order"}), 400

        wo = next((w for w in work_orders if w.id == wo_id), None)
        if not wo:
            return jsonify({"error": "Work order not found"}), 404

        # Close any open entry for this operator (e.g. a pause entry)
        wo.time_entries = [
            {**e, "end_time": now} if e["operator_id"] == operator_id and e["end_time"] is None else e
            for e in wo.time_entries
        ]
        wo.time_entries.append({
            "id": str(uuid.uuid4()),
            "operator_id": operator_id,
            "operator_name": operator_name,
            "start_time": now,
            "end_time": None,
            "pause_reason": None,
        })
        wo.status = WorkOrderStatus.IN_PROGRESS
        _update_wo_active_mins(wo)

    return jsonify(_wo_to_dict(wo))


@app.route("/api/work-orders/<wo_id>/pause", methods=["POST"])
def pause_work(wo_id):
    data = request.json
    operator_id = data.get("operator_id")
    operator_name = data.get("operator_name")
    reason = data.get("reason", "Other")
    now = datetime.now().isoformat()

    with state_lock:
        wo = next((w for w in work_orders if w.id == wo_id), None)
        if not wo:
            return jsonify({"error": "Work order not found"}), 404

        wo.time_entries = [
            {**e, "end_time": now} if e["operator_id"] == operator_id and e["end_time"] is None else e
            for e in wo.time_entries
        ]
        wo.time_entries.append({
            "id": str(uuid.uuid4()),
            "operator_id": operator_id,
            "operator_name": operator_name,
            "start_time": now,
            "end_time": None,
            "pause_reason": reason,
        })

        anyone_working = any(
            e["end_time"] is None and not e.get("pause_reason")
            for e in wo.time_entries
        )
        wo.status = WorkOrderStatus.IN_PROGRESS if anyone_working else WorkOrderStatus.PAUSED
        _update_wo_active_mins(wo)

    return jsonify(_wo_to_dict(wo))


@app.route("/api/work-orders/<wo_id>/stop", methods=["POST"])
def stop_work(wo_id):
    data = request.json
    operator_id = data.get("operator_id")
    now = datetime.now().isoformat()

    with state_lock:
        wo = next((w for w in work_orders if w.id == wo_id), None)
        if not wo:
            return jsonify({"error": "Work order not found"}), 404

        wo.time_entries = [
            {**e, "end_time": now} if e["operator_id"] == operator_id and e["end_time"] is None else e
            for e in wo.time_entries
        ]

        anyone_working = any(e["end_time"] is None and not e.get("pause_reason") for e in wo.time_entries)
        anyone_paused = any(e["end_time"] is None and e.get("pause_reason") for e in wo.time_entries)

        if anyone_working:
            wo.status = WorkOrderStatus.IN_PROGRESS
        elif anyone_paused:
            wo.status = WorkOrderStatus.PAUSED
        else:
            # Only mark COMPLETED if all target units have been logged
            if wo.completed_quantity >= wo.target_quantity:
                wo.status = WorkOrderStatus.COMPLETED
            else:
                wo.status = WorkOrderStatus.PAUSED

        _update_wo_active_mins(wo)

    return jsonify(_wo_to_dict(wo))


@app.route("/api/work-orders/<wo_id>/complete-quantity", methods=["POST"])
def complete_quantity(wo_id):
    data = request.json
    amount = int(data.get("amount", 0))
    now = datetime.now().isoformat()

    with state_lock:
        wo = next((w for w in work_orders if w.id == wo_id), None)
        if not wo:
            return jsonify({"error": "Work order not found"}), 404

        wo.completed_quantity += amount
        if wo.completed_quantity >= wo.target_quantity:
            wo.status = WorkOrderStatus.COMPLETED
            # Close all open entries
            wo.time_entries = [
                {**e, "end_time": now} if e["end_time"] is None else e
                for e in wo.time_entries
            ]

        _update_wo_active_mins(wo)

    return jsonify(_wo_to_dict(wo))


@app.route("/api/work-orders/template")
def download_template():
    """Download a blank CSV template for bulk work order upload."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "style", "size", "colorway", "line", "target_quantity", "target_minutes_per_unit"])
    writer.writerow(["WO-001", "Multicam", "Medium", "OCP", "Line A", 150, 4.5])
    writer.writerow(["WO-002", "Ranger Green", "Large", "RG", "Line B", 200, 3.9])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=work_orders_template.csv"}
    )


@app.route("/api/work-orders/export-csv")
def export_wo_csv():
    """Export all work orders with full details and time breakdowns."""
    with state_lock:
        # Collect all distinct pause reasons across all WOs
        all_pause_reasons = list(PAUSE_REASONS)

        output = io.StringIO()
        writer = csv.writer(output)

        # Build header row
        header = [
            "id", "name", "style", "size", "colorway", "line",
            "target_quantity", "target_minutes_per_unit",
            "completed_quantity", "status", "actual_active_mins",
            "actual_mins_per_unit", "efficiency_pct",
        ]
        for reason in all_pause_reasons:
            header.append(f"paused_mins_{reason.lower().replace(' ', '_')}")
        writer.writerow(header)

        for wo in work_orders:
            _update_wo_active_mins(wo)

            # Tally paused minutes per reason (only closed entries)
            paused_by_reason: dict = {r: 0.0 for r in all_pause_reasons}
            for e in wo.time_entries:
                reason = e.get("pause_reason")
                if reason:
                    mins = _minutes_between(e["start_time"], e["end_time"])
                    if reason in paused_by_reason:
                        paused_by_reason[reason] = round(paused_by_reason[reason] + mins, 1)
                    else:
                        paused_by_reason[reason] = round(paused_by_reason.get(reason, 0.0) + mins, 1)

            # Actual mins per unit and efficiency
            if wo.completed_quantity > 0 and wo.total_active_mins > 0:
                actual_mins_per_unit = round(wo.total_active_mins / wo.completed_quantity, 2)
            else:
                actual_mins_per_unit = ""

            if actual_mins_per_unit != "" and wo.target_minutes_per_unit > 0:
                efficiency_pct = round((wo.target_minutes_per_unit / actual_mins_per_unit) * 100, 1)
            else:
                efficiency_pct = ""

            row = [
                wo.id, wo.name, wo.style, wo.size, wo.colorway, wo.line,
                wo.target_quantity, wo.target_minutes_per_unit,
                wo.completed_quantity, wo.status, wo.total_active_mins,
                actual_mins_per_unit, efficiency_pct,
            ]
            for reason in all_pause_reasons:
                row.append(paused_by_reason.get(reason, 0.0))
            writer.writerow(row)

        output.seek(0)
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=work_orders_export.csv"}
        )


@app.route("/api/work-orders/upload-csv", methods=["POST"])
def upload_csv():
    """Bulk-import work orders from an uploaded CSV file."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400

    stream = io.StringIO(f.stream.read().decode("utf-8-sig"))  # utf-8-sig strips BOM if present
    reader = csv.DictReader(stream)

    required_cols = {"name", "style", "size", "colorway", "line", "target_quantity", "target_minutes_per_unit"}
    if not required_cols.issubset({c.strip().lower() for c in (reader.fieldnames or [])}):
        missing = required_cols - {c.strip().lower() for c in (reader.fieldnames or [])}
        return jsonify({"error": f"Missing columns: {', '.join(missing)}"}), 400

    added = []
    skipped = []

    with state_lock:
        for i, row in enumerate(reader, start=2):  # row 1 is header
            # Normalise keys to lowercase stripped
            row = {k.strip().lower(): v.strip() for k, v in row.items()}
            name = row.get("name", "").strip()
            if not name:
                skipped.append({"row": i, "reason": "Missing name"})
                continue

            try:
                target_qty = int(row.get("target_quantity", 0) or 0)
            except ValueError:
                skipped.append({"row": i, "reason": f"Invalid target_quantity: {row.get('target_quantity')}"})
                continue

            try:
                target_mins = float(row.get("target_minutes_per_unit", 0) or 0)
            except ValueError:
                skipped.append({"row": i, "reason": f"Invalid target_minutes_per_unit: {row.get('target_minutes_per_unit')}"})
                continue

            wo = WorkOrder(
                name=name,
                style=row.get("style", ""),
                size=row.get("size", ""),
                colorway=row.get("colorway", ""),
                line=row.get("line", ""),
                target_quantity=target_qty,
                target_minutes_per_unit=target_mins,
            )
            work_orders.append(wo)
            added.append(name)

    return jsonify({
        "added": len(added),
        "skipped": len(skipped),
        "added_names": added,
        "skipped_details": skipped,
    })


@app.route("/api/operators/template")
def download_operators_template():
    """Download a blank CSV template for bulk operator upload."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "line", "role", "pin"])
    writer.writerow(["Jane Doe", "Line A", "manager", "1234"])
    writer.writerow(["John Smith", "Line A", "operator", "5678"])
    writer.writerow(["Maria Garcia", "Line B", "manager", "2468"])
    writer.writerow(["Bob Johnson", "Line B", "operator", "1357"])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=operators_template.csv"}
    )


@app.route("/api/operators/upload-csv", methods=["POST"])
def upload_operators_csv():
    """Bulk-import operators from an uploaded CSV file."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename.lower().endswith(".csv"):
        return jsonify({"error": "File must be a .csv"}), 400

    stream = io.StringIO(f.stream.read().decode("utf-8-sig"))
    reader = csv.DictReader(stream)

    required_cols = {"name", "line"}  # role is optional, defaults to "operator"
    fieldnames_lower = {c.strip().lower() for c in (reader.fieldnames or [])}
    if not required_cols.issubset(fieldnames_lower):
        missing = required_cols - fieldnames_lower
        return jsonify({"error": f"Missing columns: {', '.join(missing)}"}), 400

    added = []
    skipped = []

    with state_lock:
        existing_names = {o.name.strip().lower() for o in operators}
        for i, row in enumerate(reader, start=2):
            row = {k.strip().lower(): v.strip() for k, v in row.items()}
            name = row.get("name", "").strip()
            if not name:
                skipped.append({"row": i, "reason": "Missing name"})
                continue
            if name.lower() in existing_names:
                skipped.append({"row": i, "reason": f"Operator '{name}' already exists"})
                continue
            raw_role = row.get("role", "operator").strip().lower()
            role = raw_role if raw_role in ("operator", "manager") else "operator"
            raw_pin = str(row.get("pin", "")).strip()
            pin = raw_pin if raw_pin.isdigit() and len(raw_pin) == 4 else ""
            op = Operator(name=name, line=row.get("line", ""), role=role, pin=pin)
            operators.append(op)
            existing_names.add(name.lower())
            added.append(name)

    return jsonify({
        "added": len(added),
        "skipped": len(skipped),
        "added_names": added,
        "skipped_details": skipped,
    })


@app.route("/api/operators/<op_id>/verify-pin", methods=["POST"])
def verify_pin(op_id):
    """Verify a 4-digit PIN for an operator without exposing the stored PIN."""
    data = request.json
    entered = str(data.get("pin", "")).strip()
    with state_lock:
        op = next((o for o in operators if o.id == op_id), None)
        if not op:
            return jsonify({"error": "Operator not found"}), 404
        if not op.pin:
            # No PIN set — always valid
            return jsonify({"valid": True})
        return jsonify({"valid": entered == op.pin})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({"require_pin": require_pin})


@app.route("/api/settings", methods=["POST"])
def update_settings():
    global require_pin
    data = request.json
    if "require_pin" in data:
        require_pin = bool(data["require_pin"])
    return jsonify({"require_pin": require_pin})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
