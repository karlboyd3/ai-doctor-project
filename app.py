import os
import random
import subprocess
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "intellicare-demo-secret-2026")

@app.after_request
def no_cache(response):
    """Prevent browser from caching any page — stops back-button bypass after logout."""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

# ── DB helper ──────────────────────────────────────────────
def get_cursor():
    from pyhive import presto
    conn = presto.connect(
        host=os.getenv("DB_HOST"),
        port=443,
        username=os.getenv("DB_USER"),
        password=os.getenv("WATSONX_API_KEY"),
        catalog='iceberg_data',
        schema='healthcare',
        protocol='https',
        requests_kwargs={'verify': True}
    )
    return conn.cursor()

# ── HELPERS & DECORATORS ──────────────────────────────────

def safe_sql(text):
    return (text or "").replace("'", "''").replace("\\", "\\\\").replace("\r\n", " ").replace("\n", " ")

def dob_sql(dob_str):
    """Convert MM/DD/YYYY or YYYY-MM-DD to a Presto DATE literal, or NULL."""
    if not dob_str or not dob_str.strip():
        return "NULL"
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y'):
        try:
            from datetime import datetime as dt
            return f"DATE '{dt.strptime(dob_str.strip(), fmt).strftime('%Y-%m-%d')}'"
        except ValueError:
            continue
    return "NULL"

def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('staff_logged_in'):
            return redirect(url_for('staff_login', next=request.path))
        return f(*args, **kwargs)
    return decorated

def portal_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'patient_id' not in session:
            return redirect(url_for('portal_login'))
        return f(*args, **kwargs)
    return decorated

def ensure_patients_table():
    # Add missing columns if they don't exist yet
    for col in ['allergies', 'current_medications', 'notes', 'preferred_language']:
        try:
            cursor = get_cursor()
            cursor.execute(f"ALTER TABLE patients ADD COLUMN {col} VARCHAR")
            cursor.fetchall()
        except Exception:
            pass  # Column already exists

# ── ROUTES ────────────────────────────────────────────────

@app.route("/")
@staff_required
def index():
    try:
        cursor = get_cursor()
        cursor.execute("""
            SELECT
                COUNT(DISTINCT visit_id),
                COUNT(CASE WHEN artifact_type = 'summary' THEN 1 END),
                COUNT(CASE WHEN artifact_type = 'followup_message' THEN 1 END),
                COUNT(CASE WHEN artifact_type = 'transcript' THEN 1 END)
            FROM visit_artifacts
        """)
        row = cursor.fetchone()
        visit_count, summary_count, followup_count, transcript_count = (row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0)

        cursor.execute("""
            SELECT visit_id,
                   MAX(CASE WHEN artifact_type = 'patient_id' THEN content END) as patient_id_val
            FROM visit_artifacts
            GROUP BY visit_id
            ORDER BY visit_id DESC
            LIMIT 6
        """)
        recent_rows = cursor.fetchall()

        patient_ids = list({r[1] for r in recent_rows if r[1]})
        recent_patient_names = {}
        if patient_ids:
            id_list = "','".join(safe_sql(p) for p in patient_ids)
            cursor.execute(f"""
                SELECT patient_id, first_name, last_name
                FROM patients WHERE patient_id IN ('{id_list}')
            """)
            for pid, fn, ln in cursor.fetchall():
                recent_patient_names[pid] = f"{fn or ''} {ln or ''}".strip()

        recent_visits = [(r[0], recent_patient_names.get(r[1], '')) for r in recent_rows]

    except Exception as e:
        print(f"DB error on index: {e}")
        visit_count = summary_count = followup_count = transcript_count = 0
        recent_visits = []

    return render_template("index.html",
        visit_count=visit_count,
        summary_count=summary_count,
        followup_count=followup_count,
        transcript_count=transcript_count,
        recent_visits=recent_visits
    )

@app.route("/visits")
@staff_required
def visits():
    try:
        cursor = get_cursor()
        cursor.execute("""
            SELECT visit_id,
                   array_join(array_agg(artifact_type), ',') as types,
                   COUNT(*) as cnt,
                   MAX(CASE WHEN artifact_type = 'patient_id' THEN content END) as patient_id_val
            FROM visit_artifacts
            GROUP BY visit_id
            ORDER BY visit_id DESC
            LIMIT 100
        """)
        rows = cursor.fetchall()

        # Collect unique patient IDs and fetch names in one query
        patient_ids = list({r[3] for r in rows if r[3]})
        patient_names = {}
        if patient_ids:
            id_list = "','".join(safe_sql(p) for p in patient_ids)
            cursor.execute(f"""
                SELECT patient_id, first_name, last_name
                FROM patients WHERE patient_id IN ('{id_list}')
            """)
            for pid, fn, ln in cursor.fetchall():
                patient_names[pid] = f"{fn or ''} {ln or ''}".strip()

        visits = [(row[0], row[1] or '', row[2], patient_names.get(row[3], '')) for row in rows]
    except Exception as e:
        print(f"DB error on visits: {e}")
        visits = []
    return render_template("visits.html", visits=visits)

@app.route("/visit/<path:visit_id>")
@staff_required
def visit_detail(visit_id):
    artifacts = []
    patient = None
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT artifact_type, content, created_at
            FROM visit_artifacts
            WHERE visit_id = '{safe_sql(visit_id)}'
            ORDER BY created_at ASC
        """)
        artifacts = cursor.fetchall()

        # Check if visit is linked to a patient
        patient_link = next((a[1] for a in artifacts if a[0] == 'patient_id'), None)
        if patient_link:
            cursor.execute(f"""
                SELECT patient_id, first_name, last_name, phone FROM patients
                WHERE patient_id = '{safe_sql(patient_link)}'
            """)
            patient = cursor.fetchone()
    except Exception as e:
        print(f"DB error on visit detail: {e}")
    return render_template("visit.html", visit_id=visit_id, artifacts=artifacts, patient=patient)

@app.route("/api/visits/list")
@staff_required
def visits_list_api():
    try:
        cursor = get_cursor()
        cursor.execute("""
            SELECT visit_id,
                   MAX(CASE WHEN artifact_type = 'patient_id' THEN content END) as patient_id_val
            FROM visit_artifacts
            GROUP BY visit_id
            ORDER BY visit_id DESC
            LIMIT 200
        """)
        rows = cursor.fetchall()

        patient_ids = list({r[1] for r in rows if r[1]})
        patient_names = {}
        if patient_ids:
            id_list = "','".join(safe_sql(p) for p in patient_ids)
            cursor.execute(f"""
                SELECT patient_id, first_name, last_name
                FROM patients WHERE patient_id IN ('{id_list}')
            """)
            for pid, fn, ln in cursor.fetchall():
                patient_names[pid] = f"{fn or ''} {ln or ''}".strip()

        result = [
            {"visit_id": r[0], "patient_name": patient_names.get(r[1], '')}
            for r in rows
        ]
        return jsonify(result)
    except Exception as e:
        return jsonify([])

@app.route("/pipeline")
@staff_required
def pipeline_page():
    return render_template("pipeline.html")

@app.route("/api/run-pipeline", methods=["POST"])
@staff_required
def run_pipeline_api():
    """Runs pipeline.py as a subprocess and returns result."""
    visit_id = (request.json or {}).get("visit_id") or request.form.get("visit_id", "")
    visit_id = visit_id.strip()
    try:
        cmd = [os.path.join(os.path.dirname(__file__), "venv", "Scripts", "python.exe"), "pipeline.py"]
        if visit_id:
            cmd.append(visit_id)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        output = result.stdout + result.stderr

        if result.returncode == 0:
            # Extract visit ID from output
            visit_id = "unknown"
            for line in output.splitlines():
                if "Processing visit:" in line:
                    visit_id = line.split("Processing visit:")[-1].strip()
                    break
            return jsonify({"success": True, "visit_id": visit_id, "log": output})
        else:
            return jsonify({"success": False, "error": result.stderr[:500]})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Pipeline timed out after 120 seconds"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/send-email/<path:visit_id>", methods=["POST"])
@staff_required
def send_email(visit_id):
    email = request.form.get("email", "").strip()
    artifact_type = request.form.get("artifact_type", "summary")

    if not email:
        flash("Please enter an email address.")
        return redirect(url_for("visit_detail", visit_id=visit_id))

    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT content FROM visit_artifacts
            WHERE visit_id = '{safe_sql(visit_id)}'
            AND artifact_type = '{safe_sql(artifact_type)}'
            LIMIT 1
        """)
        row = cursor.fetchone()

        if not row:
            flash(f"No {artifact_type} found for this visit.")
            return redirect(url_for("visit_detail", visit_id=visit_id))

        content = row[0]

        subject_map = {
            'summary':                  f"Your Visit Summary — {visit_id}",
            'followup_message':         f"Follow-up from Your Visit — {visit_id}",
            'soap_note':                f"SOAP Note — {visit_id}",
            'referral_letter':          f"Referral Letter — {visit_id}",
            'sick_note':                f"Medical Certificate — {visit_id}",
            'prescription_summary':     f"Prescription Summary — {visit_id}",
            'discharge_instructions':   f"Discharge Instructions — {visit_id}",
        }
        subject = subject_map.get(artifact_type, f"IntelliCare Document — {visit_id}")

        from email_agent import send_patient_email
        send_patient_email(email, subject, content, visit_id)
        flash(f"Email sent successfully to {email}!")
    except Exception as e:
        flash(f"Email error: {str(e)}")

    return redirect(url_for("visit_detail", visit_id=visit_id))

@app.route("/send-sms/<path:visit_id>", methods=["POST"])
@staff_required
def send_sms(visit_id):
    """Send SMS for a specific visit."""
    phone = request.form.get("phone")
    message_type = request.form.get("message_type", "patient_summary")

    if not phone:
        flash("Please enter a phone number.")
        return redirect(url_for("visit_detail", visit_id=visit_id))

    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT content FROM visit_artifacts
            WHERE visit_id = '{safe_sql(visit_id)}'
            AND artifact_type = '{safe_sql(message_type)}'
            LIMIT 1
        """)
        row = cursor.fetchone()

        if not row:
            flash(f"No {message_type} found for visit {visit_id}.")
            return redirect(url_for("visit_detail", visit_id=visit_id))

        content = row[0]

        from sms_agent import send_patient_sms
        send_patient_sms(phone, content, visit_id)

        flash(f"SMS sent successfully to {phone}!")
    except Exception as e:
        flash(f"SMS error: {str(e)}")

    return redirect(url_for("visit_detail", visit_id=visit_id))

@app.route("/api/visit/<path:visit_id>/generate-paperwork", methods=["POST"])
@staff_required
def generate_paperwork(visit_id):
    data = request.json or {}
    doc_types = data.get("doc_types", [])
    if not doc_types:
        return jsonify({"success": False, "error": "No document types specified."})
    doc_types_arg = ",".join(doc_types)
    python_exe = os.path.join(os.path.dirname(__file__), "venv", "Scripts", "python.exe")
    log_path = os.path.join(os.path.dirname(__file__), "paperwork.log")
    log_file = open(log_path, "w")
    subprocess.Popen(
        [python_exe, "paperwork.py", safe_sql(visit_id), doc_types_arg],
        stdout=log_file,
        stderr=log_file,
        cwd=os.path.dirname(__file__)
    )
    return jsonify({"success": True})

@app.route("/api/visit/<path:visit_id>/update-artifact", methods=["POST"])
@staff_required
def update_artifact(visit_id):
    artifact_type = request.form.get("artifact_type", "")
    content       = request.form.get("content", "")
    allowed = {'summary', 'followup_message', 'summary_es', 'followup_es',
               'soap_note', 'referral_letter', 'sick_note', 'prescription_summary', 'discharge_instructions'}
    if artifact_type not in allowed:
        return jsonify({"success": False, "error": "That artifact type cannot be edited."})
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            UPDATE visit_artifacts SET content = '{safe_sql(content)}'
            WHERE visit_id = '{safe_sql(visit_id)}' AND artifact_type = '{artifact_type}'
        """)
        cursor.fetchall()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/visit/<path:visit_id>")
@staff_required
def visit_api(visit_id):
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT artifact_type, content
            FROM visit_artifacts WHERE visit_id = '{safe_sql(visit_id)}'
        """)
        data = {row[0]: row[1] for row in cursor.fetchall()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── TRANSCRIBE ────────────────────────────────────────────
@app.route("/transcribe")
@staff_required
def transcribe_page():
    return render_template("transcribe.html")

@app.route("/api/transcribe", methods=["POST"])
@staff_required
def transcribe_api():
    if 'audio' not in request.files:
        return jsonify({"success": False, "error": "No audio file provided"})

    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"success": False, "error": "No file selected"})

    visit_id = request.form.get("visit_id", "").strip()
    if not visit_id:
        visit_id = "V_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    # Remove characters that break URLs
    visit_id = visit_id.replace("/", "-").replace("\\", "-").replace(" ", "_")
    language    = request.form.get("language", "en")
    environment = request.form.get("environment", "clinic")
    patient_id_link = safe_sql(request.form.get("patient_id", "").strip())

    upload_dir = os.path.join("patient_files", visit_id)
    os.makedirs(upload_dir, exist_ok=True)
    filename = secure_filename(audio_file.filename)
    file_path = os.path.join(upload_dir, filename)
    audio_file.save(file_path)

    try:
        from stt_agent import transcribe_audio
        transcript = transcribe_audio(file_path, visit_id, language=language, environment=environment)

        # Insert transcript into DB from app.py (same connection context as all other DB writes)
        cursor = get_cursor()
        cursor.execute(f"""
            INSERT INTO visit_artifacts VALUES (
                'A_TRANS_{visit_id}',
                '{visit_id}',
                'transcript',
                '{safe_sql(transcript)}',
                CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
            )
        """)
        cursor.fetchall()  # force Presto to complete the INSERT before continuing
        print(f"Transcript saved to DB for visit {visit_id}")

        # Auto-link visit to patient if initiated from a patient profile
        if patient_id_link:
            cursor.execute(f"""
                INSERT INTO visit_artifacts VALUES (
                    'A_PAT_{visit_id}', '{visit_id}', 'patient_id',
                    '{patient_id_link}',
                    CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
                )
            """)
            cursor.fetchall()
            print(f"Visit {visit_id} auto-linked to patient {patient_id_link}")

        # Auto-run pipeline in background for this specific visit
        python_exe = os.path.join(os.path.dirname(__file__), "venv", "Scripts", "python.exe")
        subprocess.Popen(
            [python_exe, "pipeline.py", visit_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"Pipeline started in background for visit {visit_id}")

        preview = transcript[:300] + ("..." if len(transcript) > 300 else "")
        return jsonify({"success": True, "visit_id": visit_id, "preview": preview})
    except Exception as e:
        print(f"Transcribe error: {e}")
        return jsonify({"success": False, "error": str(e)})

# ── STAFF AUTH ────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def staff_login():
    if session.get('staff_logged_in'):
        return redirect(url_for('index'))
    error = None
    next_url = request.args.get('next') or request.form.get('next') or '/'
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if (username == os.getenv("ADMIN_USER", "admin") and
                password == os.getenv("ADMIN_PASS", "intellicare")):
            session['staff_logged_in'] = True
            session['staff_user'] = username
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("login.html", error=error, next=next_url)

@app.route("/logout")
def staff_logout():
    session.pop('staff_logged_in', None)
    session.pop('staff_user', None)
    return redirect(url_for('staff_login'))

# ── PATIENTS ──────────────────────────────────────────────

def ensure_patients_table():
    # Add missing columns if they don't exist yet
    for col in ['allergies', 'current_medications', 'notes', 'preferred_language']:
        try:
            cursor = get_cursor()
            cursor.execute(f"ALTER TABLE patients ADD COLUMN {col} VARCHAR")
            cursor.fetchall()
        except Exception:
            pass  # Column already exists

@app.route("/patients")
@staff_required
def patients_page():
    ensure_patients_table()
    try:
        cursor = get_cursor()
        cursor.execute("""
            SELECT patient_id, first_name, last_name, dob, sex, phone, email, address, pin, allergies, current_medications, notes, preferred_language
            FROM patients
            ORDER BY last_name ASC
        """)
        patients = cursor.fetchall()
    except Exception as e:
        print(f"DB error on patients: {e}")
        patients = []
    return render_template("patients.html", patients=patients)

@app.route("/patients/new", methods=["POST"])
@staff_required
def new_patient():
    patient_id  = "P_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pin         = str(random.randint(100000, 999999))
    first_name  = safe_sql(request.form.get("first_name", ""))
    last_name   = safe_sql(request.form.get("last_name", ""))
    dob         = dob_sql(request.form.get("dob", ""))
    sex         = safe_sql(request.form.get("sex", ""))
    phone       = safe_sql(request.form.get("phone", ""))
    email       = safe_sql(request.form.get("email", ""))
    address     = safe_sql(request.form.get("address", ""))
    allergies   = safe_sql(request.form.get("allergies", ""))
    medications = safe_sql(request.form.get("current_medications", ""))
    notes       = safe_sql(request.form.get("notes", ""))
    lang        = 'es' if request.form.get("preferred_language") == 'es' else ''
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            INSERT INTO patients (patient_id, first_name, last_name, dob, sex, phone, email, address, pin, allergies, current_medications, notes, preferred_language)
            VALUES ('{patient_id}', '{first_name}', '{last_name}', {dob}, '{sex}', '{phone}', '{email}', '{address}', '{pin}', '{allergies}', '{medications}', '{notes}', '{lang}')
        """)
        cursor.fetchall()
        flash(f"Patient {first_name} {last_name} added. Portal PIN: {pin}")
        return redirect(url_for("patient_detail", patient_id=patient_id))
    except Exception as e:
        flash(f"Error adding patient: {str(e)}")
        return redirect(url_for("patients_page"))

@app.route("/patient/<patient_id>")
@staff_required
def patient_detail(patient_id):
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT patient_id, first_name, last_name, dob, sex, phone, email, address, pin, allergies, current_medications, notes, preferred_language
            FROM patients WHERE patient_id = '{safe_sql(patient_id)}'
        """)
        patient = cursor.fetchone()

        cursor.execute(f"""
            SELECT visit_id FROM visit_artifacts
            WHERE artifact_type = 'patient_id' AND content = '{safe_sql(patient_id)}'
            ORDER BY created_at DESC
        """)
        visit_ids = [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"DB error on patient detail: {e}")
        patient = None
        visit_ids = []
    return render_template("patient.html", patient=patient, visit_ids=visit_ids)

@app.route("/patient/<patient_id>/edit", methods=["POST"])
@staff_required
def edit_patient(patient_id):
    first_name  = safe_sql(request.form.get("first_name", ""))
    last_name   = safe_sql(request.form.get("last_name", ""))
    dob         = dob_sql(request.form.get("dob", ""))
    sex         = safe_sql(request.form.get("sex", ""))
    phone       = safe_sql(request.form.get("phone", ""))
    email       = safe_sql(request.form.get("email", ""))
    address     = safe_sql(request.form.get("address", ""))
    allergies   = safe_sql(request.form.get("allergies", ""))
    medications = safe_sql(request.form.get("current_medications", ""))
    notes       = safe_sql(request.form.get("notes", ""))
    lang        = 'es' if request.form.get("preferred_language") == 'es' else ''
    regen_pin   = request.form.get("regen_pin") == "1"
    try:
        cursor = get_cursor()
        if regen_pin:
            new_pin = str(random.randint(100000, 999999))
            cursor.execute(f"""
                UPDATE patients SET
                    first_name = '{first_name}', last_name = '{last_name}', dob = {dob},
                    sex = '{sex}', phone = '{phone}', email = '{email}', address = '{address}',
                    allergies = '{allergies}', current_medications = '{medications}',
                    notes = '{notes}', preferred_language = '{lang}', pin = '{new_pin}'
                WHERE patient_id = '{safe_sql(patient_id)}'
            """)
            flash(f"Patient updated. New PIN: {new_pin}")
        else:
            cursor.execute(f"""
                UPDATE patients SET
                    first_name = '{first_name}', last_name = '{last_name}', dob = {dob},
                    sex = '{sex}', phone = '{phone}', email = '{email}', address = '{address}',
                    allergies = '{allergies}', current_medications = '{medications}',
                    notes = '{notes}', preferred_language = '{lang}'
                WHERE patient_id = '{safe_sql(patient_id)}'
            """)
            flash("Patient updated successfully!")
        cursor.fetchall()
    except Exception as e:
        flash(f"Error updating patient: {str(e)}")
    return redirect(url_for("patient_detail", patient_id=patient_id))

# ── INTAKE ────────────────────────────────────────────────
@app.route("/intake")
def intake():
    return render_template("intake.html", submitted=False)

@app.route("/intake/submit", methods=["POST"])
def intake_submit():
    ensure_patients_table()
    patient_id = "P_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pin         = str(random.randint(100000, 999999))
    first_name  = safe_sql(request.form.get("first_name", ""))
    last_name   = safe_sql(request.form.get("last_name", ""))
    dob         = dob_sql(request.form.get("dob", ""))
    sex         = safe_sql(request.form.get("sex", ""))
    phone       = safe_sql(request.form.get("phone", ""))
    email       = safe_sql(request.form.get("email", ""))
    address     = safe_sql(request.form.get("address", ""))
    allergies   = safe_sql(request.form.get("allergies", ""))
    medications = safe_sql(request.form.get("current_medications", ""))
    notes       = safe_sql(request.form.get("notes", ""))
    lang        = 'es' if request.form.get("preferred_language") == 'es' else ''
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            INSERT INTO patients (patient_id, first_name, last_name, dob, sex, phone, email, address, pin, allergies, current_medications, notes, preferred_language)
            VALUES ('{patient_id}', '{first_name}', '{last_name}', {dob}, '{sex}', '{phone}', '{email}', '{address}', '{pin}', '{allergies}', '{medications}', '{notes}', '{lang}')
        """)
        cursor.fetchall()
        return render_template("intake.html", submitted=True, name=first_name, pin=pin)
    except Exception as e:
        return render_template("intake.html", submitted=False, error=str(e))

# ── PATIENT PORTAL ────────────────────────────────────────
@app.route("/portal")
def portal_login():
    if 'patient_id' in session:
        return redirect(url_for('portal_dashboard'))
    return render_template("portal_login.html", error=None)

@app.route("/portal/login", methods=["POST"])
def portal_login_submit():
    name = request.form.get("name", "").strip()
    pin  = request.form.get("pin", "").strip()
    try:
        ensure_patients_table()  # ensures preferred_language column exists
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT patient_id, first_name, last_name FROM patients
            WHERE LOWER(first_name || ' ' || last_name) = LOWER('{safe_sql(name)}')
            AND pin = '{safe_sql(pin)}'
            LIMIT 1
        """)
        patient = cursor.fetchone()
        if patient:
            session['patient_id']   = patient[0]
            session['patient_name'] = f"{patient[1]} {patient[2]}"
            return redirect(url_for('portal_dashboard'))
        else:
            return render_template("portal_login.html", error="Invalid name or PIN. Please try again.")
    except Exception as e:
        return render_template("portal_login.html", error=str(e))

@app.route("/portal/dashboard")
@portal_required
def portal_dashboard():
    patient_id = session['patient_id']
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT patient_id, first_name, last_name, dob, sex, phone, email, address, pin, allergies, current_medications, notes, preferred_language
            FROM patients WHERE patient_id = '{safe_sql(patient_id)}'
        """)
        patient = cursor.fetchone()

        cursor.execute(f"""
            SELECT visit_id, created_at FROM visit_artifacts
            WHERE artifact_type = 'patient_id' AND content = '{safe_sql(patient_id)}'
            ORDER BY created_at DESC
        """)
        linked = cursor.fetchall()

        visits = []
        for vid, ts in linked:
            cursor.execute(f"""
                SELECT array_join(array_agg(artifact_type), ',')
                FROM visit_artifacts WHERE visit_id = '{safe_sql(vid)}'
            """)
            row = cursor.fetchone()
            types = row[0] if row else ''
            if 'summary' in types or 'followup_message' in types:
                visits.append((vid, ts, types))
    except Exception as e:
        print(f"Portal dashboard error: {e}")
        patient = None
        visits = []
    return render_template("portal_dashboard.html", patient=patient, visits=visits)

@app.route("/portal/visit/<path:visit_id>")
@portal_required
def portal_visit(visit_id):
    patient_id = session['patient_id']
    patient_language = 'en'
    try:
        cursor = get_cursor()
        # Verify visit belongs to this patient
        cursor.execute(f"""
            SELECT content FROM visit_artifacts
            WHERE visit_id = '{safe_sql(visit_id)}' AND artifact_type = 'patient_id' LIMIT 1
        """)
        link = cursor.fetchone()
        if not link or link[0] != patient_id:
            return redirect(url_for('portal_dashboard'))

        # Get patient's language preference
        cursor.execute(f"""
            SELECT preferred_language FROM patients
            WHERE patient_id = '{safe_sql(patient_id)}' LIMIT 1
        """)
        lang_row = cursor.fetchone()
        if lang_row and lang_row[0] == 'es':
            patient_language = 'es'

        cursor.execute(f"""
            SELECT artifact_type, content, created_at FROM visit_artifacts
            WHERE visit_id = '{safe_sql(visit_id)}'
            AND artifact_type IN ('summary', 'followup_message', 'summary_es', 'followup_es')
            ORDER BY created_at ASC
        """)
        raw = cursor.fetchall()

        artifacts = []
        for atype, content, ts in raw:
            if atype == 'summary' and 'PATIENT SUMMARY:' in content:
                start = content.index('PATIENT SUMMARY:') + len('PATIENT SUMMARY:')
                content = content[start:]
                if 'CLINICAL SUMMARY:' in content:
                    content = content[:content.index('CLINICAL SUMMARY:')]
                content = content.strip()
            artifacts.append((atype, content, ts))
    except Exception as e:
        print(f"Portal visit error: {e}")
        artifacts = []
    return render_template("portal_visit.html", visit_id=visit_id, artifacts=artifacts, patient_language=patient_language)

@app.route("/portal/logout")
def portal_logout():
    session.pop('patient_id', None)
    session.pop('patient_name', None)
    return redirect(url_for('portal_login'))

@app.route("/visit/<path:visit_id>/delete", methods=["POST"])
@staff_required
def delete_visit(visit_id):
    try:
        cursor = get_cursor()
        cursor.execute(f"DELETE FROM visit_artifacts WHERE visit_id = '{safe_sql(visit_id)}'")
        cursor.fetchall()
        flash(f"Visit {visit_id} deleted.")
    except Exception as e:
        flash(f"Error deleting visit: {str(e)}")
    return redirect(url_for("visits"))

@app.route("/patient/<patient_id>/delete", methods=["POST"])
@staff_required
def delete_patient(patient_id):
    try:
        cursor = get_cursor()
        cursor.execute(f"DELETE FROM patients WHERE patient_id = '{safe_sql(patient_id)}'")
        cursor.fetchall()
        flash("Patient record deleted.")
    except Exception as e:
        flash(f"Error deleting patient: {str(e)}")
    return redirect(url_for("patients_page"))

# ── APPOINTMENTS ─────────────────────────────────────────

@app.route("/api/appointments")
@staff_required
def get_appointments_api():
    from appointments import get_appointments_in_range
    from datetime import date
    start_str = request.args.get('start', date.today().isoformat())
    end_str   = request.args.get('end',   date.today().isoformat())
    try:
        start = date.fromisoformat(start_str)
        end   = date.fromisoformat(end_str)
        return jsonify(get_appointments_in_range(start, end))
    except Exception:
        return jsonify([])


@app.route("/api/send-appointment-options/<path:visit_id>", methods=["POST"])
@staff_required
def send_appointment_options(visit_id):
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("Please enter a phone number.")
        return redirect(url_for("visit_detail", visit_id=visit_id))

    patient_name = "Patient"
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            SELECT p.first_name, p.last_name
            FROM patients p
            JOIN visit_artifacts va ON va.content = p.patient_id
            WHERE va.visit_id = '{safe_sql(visit_id)}' AND va.artifact_type = 'patient_id'
            LIMIT 1
        """)
        row = cursor.fetchone()
        if row:
            patient_name = f"{row[0] or ''} {row[1] or ''}".strip()
    except Exception:
        pass

    from appointments import get_available_slots, set_pending_options, fmt_slot
    slots = get_available_slots(3)
    set_pending_options(phone, slots, patient_name, visit_id)

    lines = "\n".join(f"{i}. {fmt_slot(s)}" for i, s in enumerate(slots, 1))
    body = (f"IntelliCare: Hi {patient_name}, please choose a follow-up appointment:\n\n"
            f"{lines}\n\nReply 1, 2, or 3 to confirm.")

    try:
        from twilio.rest import Client
        twilio = Client(os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN"))
        twilio.messages.create(body=body, from_=os.getenv("TWILIO_FROM"), to=phone)
        flash(f"Appointment options sent to {phone}!")
    except Exception as e:
        flash(f"SMS error: {str(e)}")

    return redirect(url_for("visit_detail", visit_id=visit_id))


@app.route("/api/sms-reply", methods=["POST"])
def sms_reply_webhook():
    """Twilio inbound SMS webhook — patient replies 1/2/3 to book appointment."""
    from appointments import get_pending_options, book_appointment, clear_pending_options, fmt_slot
    from_phone = request.form.get("From", "")
    body       = request.form.get("Body", "").strip()

    def twiml(msg):
        return (f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<Response><Message>{msg}</Message></Response>',
                200, {'Content-Type': 'text/xml'})

    pending = get_pending_options(from_phone)
    if not pending:
        return twiml("No pending appointment request found. Please contact the office.")

    if body not in ('1', '2', '3'):
        return twiml("Please reply with 1, 2, or 3 to choose your appointment time.")

    options = pending['options']
    choice  = int(body) - 1
    if choice >= len(options):
        return twiml("Invalid choice. Please reply with 1, 2, or 3.")

    chosen = datetime.fromisoformat(options[choice])
    book_appointment(chosen, pending['patient_name'], from_phone, pending['visit_id'])
    clear_pending_options(from_phone)

    return twiml(f"Your appointment is confirmed for {fmt_slot(chosen)}. See you then! — IntelliCare")


@app.route("/patient/<patient_id>/link-visit", methods=["POST"])
@staff_required
def link_visit_to_patient(patient_id):
    visit_id = request.form.get("visit_id", "").strip()
    if not visit_id:
        flash("Please enter a visit ID.")
        return redirect(url_for("patient_detail", patient_id=patient_id))
    try:
        cursor = get_cursor()
        cursor.execute(f"""
            INSERT INTO visit_artifacts VALUES (
                'A_PAT_{safe_sql(visit_id)}', '{safe_sql(visit_id)}', 'patient_id',
                '{safe_sql(patient_id)}',
                CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
            )
        """)
        cursor.fetchall()
        flash(f"Visit {visit_id} linked successfully!")
    except Exception as e:
        flash(f"Error linking visit: {str(e)}")
    return redirect(url_for("patient_detail", patient_id=patient_id))

@app.route("/api/schema/patients")
@staff_required
def patients_schema():
    try:
        cursor = get_cursor()
        cursor.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'patients'
            ORDER BY ordinal_position
        """)
        cols = cursor.fetchall()
        return jsonify({"columns": [{"name": c[0], "type": c[1]} for c in cols]})
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    app.run(debug=True, port=5000)