import os
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from dotenv import load_dotenv
from ibm_watsonx_ai import Credentials
from ibm_watsonx_ai.foundation_models import ModelInference
from db import get_cursor

load_dotenv()

def cap_words(text, max_words):
    """Hard-truncate at max_words, ending on the last complete sentence."""
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = ' '.join(words[:max_words])
    # End on the last sentence boundary
    for punct in ['. ', '! ', '? ']:
        last = truncated.rfind(punct)
        if last != -1:
            return truncated[:last + 1].strip()
    return truncated.strip()

# Casual / informal phrases that should never appear in clinical documentation
_CASUAL_PATTERNS = [
    r'\btell (?:the )?patient to\b',
    r'\blots of\b',
    r'\btake it easy\b',
    r'\bjust (?:take|use|do)\b',
    r'\bbasically\b',
    r'\bokay\??\b',
    r'\bok\??\b',
    r'\bsee you soon\b',
    r'\btake care\b',
    r'\bdon\'t forget\b',
    r'\bpretty\b',
    r'\bkinda\b',
    r'\bsorta\b',
]

def validate_clinical_text(text):
    """
    Two-layer clinical output validator:
    1. Fast regex pass — catches known casual phrases and flags them.
    2. Granite judge pass — asks the model to identify any sentence that
       either (a) contradicts the source or (b) uses non-clinical language,
       and returns a cleaned version.
    Returns (cleaned_text, issues_found: list[str])
    """
    import re
    issues = []

    # Layer 1: regex scan for casual language
    for pattern in _CASUAL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            issues.append(f"Casual phrase detected: {pattern}")

    if not issues:
        return text, []  # fast path — no problems found

    # Layer 2: Granite rewrite to fix the flagged issues
    judge_prompt = (
        "<|system|>You are a clinical documentation editor. You will receive clinical notes "
        "that may contain casual or unprofessional language. Your job is to rewrite any "
        "informal phrases using formal clinical language, while keeping all medical facts "
        "exactly as stated. Do not add or remove any clinical information. "
        "Output only the corrected clinical notes — no explanations.<|end|>\n"
        "<|user|>Rewrite any informal phrases in the following clinical notes using proper "
        "clinical terminology. Keep all facts identical. Output the corrected notes only:\n\n"
        f"{text}<|end|>\n"
        "<|assistant|>"
    )
    corrected = model.generate_text(
        prompt=judge_prompt,
        params={
            "max_new_tokens": 400,
            "repetition_penalty": 1.1,
            "temperature": 0.2,
            "top_p": 0.9,
            "stop_sequences": ["<|user|>", "<|system|>", "---"]
        }
    ).strip()

    return corrected, issues

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
# STEP 1: GET TRANSCRIPT
# ========================
target_visit_id = sys.argv[1] if len(sys.argv) > 1 else None

cursor = get_cursor()
if target_visit_id:
    cursor.execute(f"""
        SELECT visit_id, content
        FROM visit_artifacts
        WHERE artifact_type = 'transcript'
        AND visit_id = '{target_visit_id}'
        LIMIT 1
    """)
else:
    cursor.execute("""
        SELECT visit_id, content
        FROM visit_artifacts
        WHERE artifact_type = 'transcript'
        ORDER BY created_at DESC
        LIMIT 1
    """)

row = cursor.fetchone()
if not row:
    msg = f"No transcript found for visit {target_visit_id}." if target_visit_id else "No transcripts found in database."
    print(msg)
    exit()

visit_id   = row[0]
transcript = row[1]
print(f"Processing visit: {visit_id}")

# ========================
# STEP 1b: PATIENT HISTORY
# ========================
prior_history = ""
try:
    cursor = get_cursor()
    cursor.execute(f"""
        SELECT content FROM visit_artifacts
        WHERE visit_id = '{visit_id}' AND artifact_type = 'patient_id' LIMIT 1
    """)
    patient_link = cursor.fetchone()

    if patient_link:
        linked_patient_id = patient_link[0]
        cursor.execute(f"""
            SELECT visit_id FROM visit_artifacts
            WHERE artifact_type = 'patient_id' AND content = '{linked_patient_id}'
            AND visit_id != '{visit_id}'
            ORDER BY created_at DESC
            LIMIT 3
        """)
        prior_visit_ids = [r[0] for r in cursor.fetchall()]

        prior_summaries = []
        for vid in prior_visit_ids:
            cursor.execute(f"""
                SELECT content FROM visit_artifacts
                WHERE visit_id = '{vid}' AND artifact_type = 'summary' LIMIT 1
            """)
            summary_row = cursor.fetchone()
            if summary_row:
                content = summary_row[0]
                # Extract just the patient-facing portion, skip the clinical block
                if 'PATIENT SUMMARY:' in content:
                    start = content.index('PATIENT SUMMARY:') + len('PATIENT SUMMARY:')
                    end = content.index('CLINICAL SUMMARY:') if 'CLINICAL SUMMARY:' in content else len(content)
                    content = content[start:end].strip()
                prior_summaries.append(f"Visit {vid}:\n{content}")

        if prior_summaries:
            prior_history = "\n\n".join(prior_summaries)
            print(f"Found {len(prior_summaries)} prior visit(s) — injecting history context.")
        else:
            print("No prior visit summaries found for this patient.")
    else:
        print("Visit not linked to a patient — running without history context.")
except Exception as e:
    print(f"Could not load patient history: {e}")

history_block = (
    f"\n\nPatient's prior visit history (for context — do not repeat this, just use it to inform your response):\n{prior_history}\n"
    if prior_history else ""
)

# ========================
# STEP 2: PATIENT SUMMARY
# ========================
patient_prompt = (
    "<|system|>You are a warm, caring medical assistant writing directly to a patient "
    "after their appointment. Use simple everyday language — no medical jargon. "
    "Be specific: use the patient's name if mentioned, name their exact condition or "
    "diagnosis, list any medications prescribed with dosage if stated, and spell out "
    "their specific next steps. If prior visit history is provided, reference relevant "
    "patterns or changes (e.g. ongoing conditions, medication adjustments). "
    "Maximum 160 words. Prose only. No bullet points.<|end|>\n"
    "<|user|>Write a personalized visit summary for this patient based on the transcript below. "
    "Address them by name if you can find it. Include: what was discussed, what was diagnosed "
    "or found, any medications or treatments prescribed, and exactly what they need to do next."
    f"{history_block}\n"
    f"Today's transcript:\n{transcript}<|end|>\n"
    "<|assistant|>Here is a summary of your visit:\n"
)

print("Running Patient Summary Agent...")
patient_summary = cap_words(model.generate_text(
    prompt=patient_prompt,
    params={
        "max_new_tokens": 220,
        "min_new_tokens": 60,
        "repetition_penalty": 1.15,
            "temperature": 0.3,
            "top_p": 0.85,
        "stop_sequences": ["<|user|>", "<|system|>", "---", "\n\n\n"]
    }
).strip(), 160)

# ========================
# STEP 3: CLINICAL NOTES
# ========================
clinical_prompt = (
    "<|system|>You are a clinical documentation specialist. Extract precise, structured "
    "clinical notes from physician-patient transcripts. Be specific and factual — include "
    "exact values (vitals, dosages, dates) where mentioned. Never invent information not "
    "explicitly stated in the transcript. Write in formal clinical language only — no casual "
    "phrasing, no colloquialisms, no informal instructions. Use standard medical terminology "
    "(e.g. 'Advise patient to limit weight-bearing activity' not 'Tell patient to stop jumping'). "
    "If a field is not mentioned in the transcript, write 'Not documented'. "
    "If prior visit history is provided, use it to populate the Relevant history field "
    "and note any changes from previous visits.<|end|>\n"
    "<|user|>Extract a structured clinical note from this transcript using exactly these fields. "
    "Write each field using formal clinical language. If the transcript does not mention a field, write 'Not documented':\n"
    "- Patient name:\n"
    "- Chief complaint:\n"
    "- Symptoms (onset, severity, duration):\n"
    "- Relevant history:\n"
    "- Vitals (if mentioned):\n"
    "- Assessment / Diagnosis:\n"
    "- Medications prescribed (name, dose, frequency):\n"
    "- Plan / Next steps:\n"
    "- Follow-up date (if mentioned):\n"
    f"{history_block}\n"
    f"Today's transcript:\n{transcript}<|end|>\n"
    "<|assistant|>"
)

print("Running Clinical Notes Agent...")
clinical_raw = cap_words(model.generate_text(
    prompt=clinical_prompt,
    params={
        "max_new_tokens": 350,
        "min_new_tokens": 80,
        "repetition_penalty": 1.15,
        "temperature": 0.3,
        "top_p": 0.85,
        "stop_sequences": ["<|user|>", "<|system|>", "---", "\n\n\n"]
    }
).strip(), 300)

clinical_summary, clinical_issues = validate_clinical_text(clinical_raw)
if clinical_issues:
    print(f"Clinical validator flagged {len(clinical_issues)} issue(s) — applied corrections.")
else:
    print("Clinical validator: output looks clean.")

# ========================
# STEP 4: FOLLOW-UP
# ========================
followup_prompt = (
    "<|system|>You are a caring medical receptionist sending a personal follow-up text "
    "to a patient 2-3 days after their visit. Write warmly and specifically — use their "
    "name, mention their exact condition, reference any medication they were given, and "
    "remind them of their specific next step or follow-up appointment. "
    "Maximum 100 words. Plain prose. No bullet points.<|end|>\n"
    "<|user|>Write a personalized follow-up message for this patient using the clinical note below. "
    "Use their name, reference their specific diagnosis and treatment, ask how they are feeling, "
    "and remind them of their next step. End with an invitation to call if anything worsens.\n\n"
    f"Clinical note:\n{clinical_summary}<|end|>\n"
    "<|assistant|>Hi"
)

print("Running Follow-up Agent...")
followup_raw = cap_words(model.generate_text(
    prompt=followup_prompt,
    params={
        "max_new_tokens": 150,
        "min_new_tokens": 40,
        "repetition_penalty": 1.15,
            "temperature": 0.3,
            "top_p": 0.85,
        "stop_sequences": ["<|user|>", "<|system|>", "---", "\n\n\n"]
    }
).strip(), 100)

followup = "Hi " + followup_raw if not followup_raw.lower().startswith("hi") else followup_raw

# Combine summary for storage (patient + clinical together)
full_summary = f"PATIENT SUMMARY:\n{patient_summary}\n\nCLINICAL SUMMARY:\n{clinical_summary}"

# ========================
# STEP 5: SAVE TO DATABASE
# ========================
def safe(text):
    return text.replace("'", "''").replace("$$", "").replace("\\", "\\\\")

cursor = get_cursor()

# Check for duplicates
cursor.execute(f"""
    SELECT COUNT(*) FROM visit_artifacts
    WHERE visit_id = '{visit_id}'
    AND artifact_type IN ('summary', 'followup_message')
""")
existing = cursor.fetchone()[0]

if existing > 0:
    print(f"Records already exist for {visit_id} — skipping insert.")
else:
    cursor.execute(f"""
        INSERT INTO visit_artifacts VALUES (
            'A_SUM_{visit_id}', '{visit_id}', 'summary',
            '{safe(full_summary)}',
            CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
        )
    """)
    cursor.fetchall()

    cursor.execute(f"""
        INSERT INTO visit_artifacts VALUES (
            'A_FOLLOW_{visit_id}', '{visit_id}', 'followup_message',
            '{safe(followup)}',
            CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
        )
    """)
    cursor.fetchall()
    print("Saved to database.")

# ========================
# STEP 6: SPANISH TRANSLATION (if patient prefers Spanish)
# ========================
patient_language = 'en'
try:
    cursor = get_cursor()
    cursor.execute(f"""
        SELECT content FROM visit_artifacts
        WHERE visit_id = '{visit_id}' AND artifact_type = 'patient_id' LIMIT 1
    """)
    patient_link = cursor.fetchone()
    if patient_link:
        cursor.execute(f"""
            SELECT preferred_language FROM patients
            WHERE patient_id = '{patient_link[0]}' LIMIT 1
        """)
        lang_row = cursor.fetchone()
        if lang_row and lang_row[0] == 'es':
            patient_language = 'es'
except Exception as e:
    print(f"Could not determine patient language: {e}")

if patient_language == 'es':
    print("Running Spanish Translation Agent...")

    def translate_to_spanish(text, label):
        trans_prompt = (
            "<|system|>You are a medical translator. Translate the following English medical text "
            "to Spanish accurately. Use warm, simple language a patient would understand. "
            "Preserve all medical details, names, dosages, and diagnoses exactly. "
            "Output ONLY the translated text — no notes, no explanations, no reasoning.<|end|>\n"
            f"<|user|>Translate this {label} to Spanish. Reply with the translation only:\n\n{text}<|end|>\n"
            "<|assistant|>"
        )
        return model.generate_text(
            prompt=trans_prompt,
            params={
                "max_new_tokens": 500,
                "repetition_penalty": 1.1,
                "stop_sequences": ["<|user|>", "<|system|>", "---", "\nAquí", "\nNota:", "\n-"]
            }
        ).strip()

    summary_es  = translate_to_spanish(patient_summary, "patient visit summary")
    followup_es = translate_to_spanish(followup, "follow-up message")

    cursor = get_cursor()
    cursor.execute(f"""
        SELECT COUNT(*) FROM visit_artifacts
        WHERE visit_id = '{visit_id}'
        AND artifact_type IN ('summary_es', 'followup_es')
    """)
    existing_es = cursor.fetchone()[0]

    if existing_es == 0:
        cursor.execute(f"""
            INSERT INTO visit_artifacts VALUES (
                'A_SUM_ES_{visit_id}', '{visit_id}', 'summary_es',
                '{safe(summary_es)}',
                CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
            )
        """)
        cursor.fetchall()

        cursor.execute(f"""
            INSERT INTO visit_artifacts VALUES (
                'A_FOLLOW_ES_{visit_id}', '{visit_id}', 'followup_es',
                '{safe(followup_es)}',
                CAST(CURRENT_TIMESTAMP AS TIMESTAMP)
            )
        """)
        cursor.fetchall()
        print("Spanish translations saved to database.")
    else:
        print(f"Spanish translations already exist for {visit_id} — skipping.")

# ========================
# DONE
# ========================
def safe_print(text):
    print(text.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))

safe_print("\nPipeline complete!")
safe_print("\n--- PATIENT SUMMARY ---")
safe_print(patient_summary)
safe_print("\n--- CLINICAL SUMMARY ---")
safe_print(clinical_summary)
safe_print("\n--- FOLLOW-UP MESSAGE ---")
safe_print(followup)
if patient_language == 'es':
    safe_print("\n--- RESUMEN DEL PACIENTE (ESPAÑOL) ---")
    safe_print(summary_es)
    safe_print("\n--- MENSAJE DE SEGUIMIENTO (ESPAÑOL) ---")
    safe_print(followup_es)