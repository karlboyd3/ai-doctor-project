import json
import os
from datetime import datetime, timedelta

APPOINTMENTS_FILE = os.path.join(os.path.dirname(__file__), 'appointments.json')
PENDING_FILE = os.path.join(os.path.dirname(__file__), 'pending_appointments.json')

VALID_HOURS = list(range(9, 17))  # 9am–4pm start (last slot 4–5pm)
WEEKDAYS = {0, 1, 2, 3, 4}       # Mon–Fri


def _load(path):
    if not os.path.exists(path):
        return {} if path == PENDING_FILE else []
    with open(path) as f:
        return json.load(f)


def _save(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def get_appointments_in_range(start_date, end_date):
    appts = _load(APPOINTMENTS_FILE)
    result = []
    for a in appts:
        dt = datetime.fromisoformat(a['datetime']).date()
        if start_date <= dt <= end_date:
            result.append(a)
    return result


def _is_available(dt):
    for a in _load(APPOINTMENTS_FILE):
        if a['datetime'] == dt.isoformat() and a['status'] == 'confirmed':
            return False
    return True


def get_available_slots(count=3):
    now = datetime.now()
    # Start from the next whole hour
    check = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    slots = []
    while len(slots) < count:
        if check.weekday() in WEEKDAYS and check.hour in VALID_HOURS:
            if _is_available(check):
                slots.append(check)
        check += timedelta(hours=1)
        # Skip past weekends
        while check.weekday() not in WEEKDAYS:
            check += timedelta(days=1)
            check = check.replace(hour=9)
    return slots


def book_appointment(dt, patient_name, patient_phone, visit_id, patient_id="", status='confirmed'):
    appts = _load(APPOINTMENTS_FILE)
    appt_id = f"APT_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    appts.append({
        'id': appt_id,
        'datetime': dt.isoformat(),
        'patient_name': patient_name,
        'patient_id': patient_id,
        'patient_phone': patient_phone,
        'visit_id': visit_id,
        'status': status,
        'booked_at': datetime.now().isoformat()
    })
    _save(APPOINTMENTS_FILE, appts)
    return appt_id


def add_followup_pending(dt, patient_name, patient_id, visit_id):
    return book_appointment(dt, patient_name, "", visit_id, patient_id=patient_id, status='pending')


def confirm_appointment(appt_id):
    appts = _load(APPOINTMENTS_FILE)
    for a in appts:
        if a['id'] == appt_id:
            a['status'] = 'confirmed'
            break
    _save(APPOINTMENTS_FILE, appts)


def delete_appointment(appt_id):
    appts = _load(APPOINTMENTS_FILE)
    appts = [a for a in appts if a['id'] != appt_id]
    _save(APPOINTMENTS_FILE, appts)


def reschedule_appointment(appt_id, new_dt):
    appts = _load(APPOINTMENTS_FILE)
    for a in appts:
        if a['id'] == appt_id:
            a['datetime'] = new_dt.isoformat()
            a['status'] = 'confirmed'
            break
    _save(APPOINTMENTS_FILE, appts)


def set_pending_options(phone, slots, patient_name, visit_id, patient_id=""):
    pending = _load(PENDING_FILE)
    pending[phone] = {
        'options': [s.isoformat() for s in slots],
        'patient_name': patient_name,
        'patient_id': patient_id,
        'visit_id': visit_id
    }
    _save(PENDING_FILE, pending)


def get_pending_options(phone):
    return _load(PENDING_FILE).get(phone)


def clear_pending_options(phone):
    pending = _load(PENDING_FILE)
    pending.pop(phone, None)
    _save(PENDING_FILE, pending)


def fmt_slot(dt):
    """Cross-platform human-readable slot string."""
    days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    hour = dt.hour
    ampm = 'AM' if hour < 12 else 'PM'
    display = hour if hour <= 12 else hour - 12
    if display == 0:
        display = 12
    return f"{days[dt.weekday()]} {months[dt.month-1]} {dt.day} at {display}:00 {ampm}"
