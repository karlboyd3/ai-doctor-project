import os
import sys
from datetime import date
from dotenv import load_dotenv
from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from db import get_cursor

load_dotenv()

# ========================
# ARGS
# visit_id   = sys.argv[1]
# doc_types  = sys.argv[2]  comma-separated, e.g. "soap_note,referral_letter"
#                           or "all" to generate everything
# ========================
if len(sys.argv) < 3:
    print("Usage: paperwork.py <visit_id> <doc_types>")
    exit(1)

visit_id  = sys.argv[1]
requested = sys.argv[2].strip().lower()

ALL_TYPES = ['soap_note', 'referral_letter', 'sick_note', 'prescription_summary', 'discharge_instructions']
doc_types = ALL_TYPES if requested == 'all' else [t.strip() for t in requested.split(',') if t.strip() in ALL_TYPES]

if not doc_types:
    print("No valid document types specified.")
    exit(1)

print(f"Generating paperwork for visit {visit_id}: {', '.join(doc_types)}")

# ========================
# CONNECT TO watsonx.ai
# ========================
creds = Credentials(
    api_key=os.getenv("WATSONX_API_KEY"),
    url=os.getenv("WATSONX_URL")
)

model = ModelInference(
    model_id="ibm/granite-4-h-small",
    credentials=creds,
    project_id=os.getenv("WATSONX_PROJECT_ID")
)

# ========================
# FETCH TRANSCRIPT
# ========================
cursor = get_cursor()

# Prefer clinical summary as input — it's already cleaned and structured by the pipeline.
# Fall back to raw transcript if summary doesn't exist yet.
cursor.execute(f"""
    SELECT content FROM visit_artifacts
    WHERE visit_id = '{visit_id}' AND artifact_type = 'summary' LIMIT 1
""")
row = cursor.fetchone()

if row:
    # Extract just the clinical summary portion (strip patient-facing section)
    content = row[0]
    if 'CLINICAL SUMMARY:' in content:
        transcript = content[content.index('CLINICAL SUMMARY:') + len('CLINICAL SUMMARY:'):].strip()
    else:
        transcript = content.strip()
    print("Using clinical summary as input.")
else:
    # Fall back to raw transcript
    cursor.execute(f"""
        SELECT content FROM visit_artifacts
        WHERE visit_id = '{visit_id}' AND artifact_type = 'transcript' LIMIT 1
    """)
    row = cursor.fetchone()
    if not row:
        print(f"No transcript or summary found for visit {visit_id}.")
        exit(1)
    transcript = row[0]
    print("No summary found — falling back to raw transcript.")

# ========================
# FETCH PATIENT INFO
# ========================
patient_info = ""
try:
    cursor = get_cursor()
    cursor.execute(f"""
        SELECT content FROM visit_artifacts
        WHERE visit_id = '{visit_id}' AND artifact_type = 'patient_id' LIMIT 1
    """)
    link = cursor.fetchone()
    if link:
        cursor.execute(f"""
            SELECT first_name, last_name, dob, sex, allergies, current_medications
            FROM patients WHERE patient_id = '{link[0]}' LIMIT 1
        """)
        p = cursor.fetchone()
        if p:
            patient_info = (
                f"Patient: {p[0]} {p[1]}\n"
                f"DOB: {p[2] or 'Unknown'}\n"
                f"Sex: {p[3] or 'Unknown'}\n"
                f"Allergies: {p[4] or 'None on file'}\n"
                f"Current medications: {p[5] or 'None on file'}"
            )
except Exception as e:
    print(f"Could not load patient info: {e}")

today = date.today().strftime("%B %d, %Y")  # e.g. April 16, 2026
patient_block = f"\n\nToday's date: {today}\nPatient profile:\n{patient_info}\n" if patient_info else f"\n\nToday's date: {today}\n"

# ========================
# HELPER
# ========================
def safe(text):
    return text.replace("'", "''").replace("$$", "").replace("\\", "\\\\")

def clean_output(text):
    """Strip lines where >40% of words exceed 18 chars (long-word gibberish)."""
    lines = text.split('\n')
    clean = []
    for line in lines:
        words = line.split()
        if words:
            long_words = sum(1 for w in words if len(w) > 18)
            if len(words) > 2 and long_words / len(words) > 0.4:
                break
        clean.append(line)
    return '\n'.join(clean).strip()

def cap_words(text, max_words):
    """Hard-truncate at max_words, ending on the last complete sentence."""
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = ' '.join(words[:max_words])
    for punct in ['. ', '! ', '? ']:
        last = truncated.rfind(punct)
        if last != -1:
            return truncated[:last + 1].strip()
    return truncated.strip()

def generate(prompt, max_tokens=500, min_tokens=60, extra_stops=None):
    stops = ["<|user|>", "<|system|>", "---"]
    if extra_stops:
        stops += extra_stops
    stops = stops[:6]  # watsonx hard limit is 6 stop sequences
    raw = model.generate_text(
        prompt=prompt,
        params={
            "max_new_tokens": max_tokens,
            "min_new_tokens": min_tokens,
            "repetition_penalty": 1.15,
            "temperature": 0.3,
            "top_p": 0.85,
            "stop_sequences": stops
        }
    ).strip()
    return clean_output(raw)

def save_artifact(artifact_id, artifact_type, content):
    cursor = get_cursor()
    # Check for existing
    cursor.execute(f"""
        SELECT COUNT(*) FROM visit_artifacts
        WHERE visit_id = '{visit_id}' AND artifact_type = '{artifact_type}'
    """)
    if cursor.fetchone()[0] > 0:
        cursor.execute(f"""
            UPDATE visit_artifacts SET content = '{safe(content)}'
            WHERE visit_id = '{visit_id}' AND artifact_type = '{artifact_type}'
        """)
        cursor.fetchall()
        print(f"Updated existing {artifact_type}.")
    else:
        cursor.execute(f"""
            INSERT INTO visit_artifacts VALUES (
                '{artifact_id}', '{visit_id}', '{artifact_type}',
                '{safe(content)}',
                CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
            )
        """)
        cursor.fetchall()
        print(f"Saved {artifact_type}.")

# ========================
# TEMPLATES
# ========================

TEMPLATES = {
    'soap_note': """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SOAP NOTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
S — SUBJECTIVE
  Chief Complaint      : {chief_complaint}
  History of Illness   : {history_of_illness}
  Past Medical History : {past_medical_history}
  Current Medications  : {current_medications}
  Allergies            : {allergies}

O — OBJECTIVE
  Vitals               : {vitals}
  Physical Exam        : {physical_exam}
  Lab / Imaging        : {lab_imaging}

A — ASSESSMENT
  Diagnosis            : {diagnosis}
  Differential         : {differential}

P — PLAN
  Treatment            : {treatment}
  Medications          : {medications_prescribed}
  Patient Education    : {patient_education}
  Follow-up            : {followup}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",

    'referral_letter': """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFERRAL LETTER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Date         : {date}
To           : {specialist}
Re           : {patient_name} | DOB: {dob}

Dear {specialist},

I am writing to refer {patient_name} for evaluation regarding {reason_for_referral}.

CLINICAL HISTORY:
{clinical_history}

RELEVANT FINDINGS:
{relevant_findings}

CURRENT MEDICATIONS:
{current_medications}

REQUEST:
{request}

Please do not hesitate to contact our office with any questions.

Sincerely,
_________________________________
Referring Physician
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",

    'sick_note': """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MEDICAL CERTIFICATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Patient Name  : {patient_name}
Date of Visit : {date_of_visit}
Condition     : {condition}
Leave Period  : {leave_period}
Restrictions  : {restrictions}
Return Date   : {return_date}

This certifies that the above-named patient is under my care and is
medically advised to {leave_period} due to the condition noted above.

Physician Signature: _________________________________
Date: _______________
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",

    'prescription_summary': """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRESCRIPTION SUMMARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Patient       : {patient_name}
Date          : {date}
Allergies     : {allergies}

MEDICATIONS PRESCRIBED:
{medications}

PHARMACY NOTES:
{pharmacy_notes}

Physician Signature: _________________________________
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",

    'discharge_instructions': """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DISCHARGE INSTRUCTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Patient       : {patient_name}
Date          : {date}

1. YOUR DIAGNOSIS
{diagnosis}

2. YOUR MEDICATIONS
{medications}

3. WHAT TO DO AT HOME
{home_care}

4. WARNING SIGNS — CALL US OR GO TO THE ER IF:
{warning_signs}

5. YOUR FOLLOW-UP APPOINTMENT
{followup}

Questions? Call our office anytime.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""",
}

# ========================
# AGENTS
# ========================

if 'soap_note' in doc_types:
    print("Running SOAP Note Agent...")
    prompt = (
        "<|system|>You are a clinical documentation specialist. "
        "Fill in the SOAP note template below using ONLY information explicitly stated in the transcript. "
        "Replace each {field} with the correct value from the transcript. "
        "If a field has no information in the transcript, write 'Not documented'. "
        "Do not infer, speculate, or add anything not directly mentioned. "
        "Output only the completed template — nothing else.<|end|>\n"
        "<|user|>Fill in this SOAP note template using only the transcript below.\n\n"
        f"TEMPLATE:\n{TEMPLATES['soap_note']}\n"
        f"{patient_block}\n"
        f"Clinical notes:\n{transcript}<|end|>\n"
        "<|assistant|>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nSOAP NOTE\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    result = cap_words(generate(prompt, max_tokens=500, min_tokens=100), 400)
    save_artifact(f'A_SOAP_{visit_id}', 'soap_note', result)

if 'referral_letter' in doc_types:
    print("Running Referral Letter Agent...")
    prompt = (
        "<|system|>You are a physician completing a referral letter. "
        "Use ONLY information explicitly stated in the transcript. "
        "Do not infer diagnoses, add symptoms, or speculate beyond what was directly said. "
        "If the specialist type is not mentioned, write 'Appropriate Specialist'. "
        "If a field has no information, write 'Not documented'. "
        "Write the letter body only — stop immediately after 'Sincerely,' and the physician name. "
        "Do not add anything after the closing.<|end|>\n"
        "<|user|>Complete this referral letter using only the transcript. "
        "Stop writing after the closing signature.\n\n"
        f"TEMPLATE:\n{TEMPLATES['referral_letter']}\n"
        f"{patient_block}\n"
        f"Clinical notes:\n{transcript}<|end|>\n"
        "<|assistant|>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nREFERRAL LETTER\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    raw = generate(prompt, max_tokens=300, min_tokens=80, extra_stops=["Sincerely,", "Regards,", "Best regards,"])
    # Re-attach the closing since it was used as a stop sequence
    if not any(c in raw for c in ["Sincerely,", "Regards,"]):
        raw += "\n\nSincerely,\n_________________________________\nReferring Physician"
    result = cap_words(raw, 220)
    save_artifact(f'A_REF_{visit_id}', 'referral_letter', result)

if 'sick_note' in doc_types:
    print("Running Sick Note Agent...")
    prompt = (
        "<|system|>You are a physician completing a medical certificate template. "
        "Fill in each {field} using ONLY information explicitly stated in the transcript. "
        "If leave duration is not stated write 'as advised by physician'. "
        "Do not invent dates or conditions. "
        "Output only the completed template — nothing else.<|end|>\n"
        "<|user|>Fill in this medical certificate template using only the transcript below.\n\n"
        f"TEMPLATE:\n{TEMPLATES['sick_note']}\n"
        f"{patient_block}\n"
        f"Clinical notes:\n{transcript}<|end|>\n"
        "<|assistant|>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nMEDICAL CERTIFICATE\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    result = cap_words(generate(prompt, max_tokens=250, min_tokens=60), 150)
    save_artifact(f'A_SICK_{visit_id}', 'sick_note', result)

if 'prescription_summary' in doc_types:
    print("Running Prescription Summary Agent...")
    prompt = (
        "<|system|>You are a clinical pharmacist completing a prescription summary template. "
        "Fill in each {field} using ONLY medications explicitly prescribed in the transcript. "
        "If no medications were prescribed write 'No medications prescribed this visit'. "
        "Do not add medications not mentioned. "
        "Output only the completed template — nothing else.<|end|>\n"
        "<|user|>Fill in this prescription summary template using only the transcript below.\n\n"
        f"TEMPLATE:\n{TEMPLATES['prescription_summary']}\n"
        f"{patient_block}\n"
        f"Clinical notes:\n{transcript}<|end|>\n"
        "<|assistant|>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nPRESCRIPTION SUMMARY\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    result = cap_words(generate(prompt, max_tokens=300, min_tokens=40), 200)
    save_artifact(f'A_RX_{visit_id}', 'prescription_summary', result)

if 'discharge_instructions' in doc_types:
    print("Running Discharge Instructions Agent...")
    prompt = (
        "<|system|>You are a nurse completing discharge instruction template for a patient. "
        "Fill in each {field} using ONLY information from the transcript. "
        "Use plain language the patient can understand. "
        "If a section has no relevant information write 'As discussed with your doctor'. "
        "Do not add conditions or instructions not mentioned. "
        "Output only the completed template — nothing else.<|end|>\n"
        "<|user|>Fill in this discharge instructions template using only the transcript below.\n\n"
        f"TEMPLATE:\n{TEMPLATES['discharge_instructions']}\n"
        f"{patient_block}\n"
        f"Clinical notes:\n{transcript}<|end|>\n"
        "<|assistant|>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nDISCHARGE INSTRUCTIONS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    result = cap_words(generate(prompt, max_tokens=400, min_tokens=100), 350)
    save_artifact(f'A_DC_{visit_id}', 'discharge_instructions', result)

print(f"\nPaperwork complete for visit {visit_id}!")
