import os
import ssl
import re
from dotenv import load_dotenv
from ibm_watson import SpeechToTextV1
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator

ssl._create_default_https_context = ssl._create_unverified_context
load_dotenv()

def clean_transcript(text):
    """
    Remove STT hallucination that appears at the end of recordings.
    Watson STT sometimes generates nonsense words when audio degrades or
    trailing noise is present. We split on sentence boundaries and stop
    at the first sentence containing suspicious patterns:
      - Words longer than 15 chars that aren't common medical terms
      - More than 30% of words in a sentence are unusually long (>12 chars)
    """
    # Common long medical/anatomical words that should not trigger the filter
    allowed_long = {
        'stabilization', 'articulation', 'articulations', 'biomechanical',
        'musculoskeletal', 'cardiovascular', 'gastrointestinal', 'inflammation',
        'contraindication', 'contraindications', 'ophthalmology', 'electrocardiogram',
        'rehabilitation', 'physiotherapy', 'recommendations', 'documentation',
        'abnormalities', 'discontinue', 'approximately', 'administration',
        'concentration', 'anterolateral', 'posterolateral', 'anteroposterior',
        'weightbearing', 'tenderness', 'prescription', 'acetaminophen',
        'acetylsalicylic', 'compartment', 'compression', 'decompression',
        'hypertension', 'hypotension', 'tachycardia', 'bradycardia',
        'differential', 'subcutaneous', 'intravenous', 'intramuscular',
    }

    # Split into sentences on . ; ! ?
    sentences = re.split(r'(?<=[.;!?])\s+', text)
    clean = []

    for sentence in sentences:
        words = sentence.split()
        if not words:
            continue
        suspicious = sum(
            1 for w in words
            if len(w) > 15 and w.lower().rstrip('.,;') not in allowed_long
        )
        long_words = sum(1 for w in words if len(w) > 12)
        # Stop if >1 word exceeds 15 chars (not whitelisted) OR
        # >40% of words in sentence are longer than 12 chars
        if suspicious > 1 or (len(words) > 4 and long_words / len(words) > 0.4):
            print(f"Transcript truncated at suspicious sentence: '{sentence[:60]}...'")
            break
        clean.append(sentence)

    return ' '.join(clean).strip()

authenticator = IAMAuthenticator(os.getenv("STT_API_KEY"))
stt = SpeechToTextV1(authenticator=authenticator)
stt.set_service_url(os.getenv("STT_URL"))

# ========================
# AUDIO ENVIRONMENT PROFILES
# background_audio_suppression: 0.0 (off) – 1.0 (max filtering)
# speech_detector_sensitivity:  0.0 (only very clear speech) – 1.0 (transcribe anything)
# ========================
AUDIO_PROFILES = {
    'office': {
        # Quiet private exam room — minimal noise, don't over-process clean audio
        'background_audio_suppression': 0.2,
        'speech_detector_sensitivity':  0.5,
        'label': 'Doctor\'s Office / Exam Room',
    },
    'clinic': {
        # Shared clinic space — hallway noise, staff chatter, moderate background
        'background_audio_suppression': 0.5,
        'speech_detector_sensitivity':  0.6,
        'label': 'Clinic / Shared Space',
    },
    'er': {
        # ER / urgent care — alarms, intercoms, multiple simultaneous conversations
        'background_audio_suppression': 0.75,
        'speech_detector_sensitivity':  0.8,
        'label': 'Emergency Room / Urgent Care',
    },
    'ambulance': {
        # Field / ambulance — sirens, wind, road noise, very loud environment
        'background_audio_suppression': 0.9,
        'speech_detector_sensitivity':  0.9,
        'label': 'Ambulance / Field',
    },
}

def _translate_to_english(spanish_text: str) -> str:
    """Use Granite to translate a Spanish transcript to English."""
    from ibm_watsonx_ai import Credentials
    from ibm_watsonx_ai.foundation_models import ModelInference

    creds = Credentials(
        api_key=os.getenv("WATSONX_API_KEY"),
        url=os.getenv("WATSONX_URL")
    )
    model = ModelInference(
        model_id="ibm/granite-4-h-small",
        credentials=creds,
        project_id=os.getenv("WATSONX_PROJECT_ID")
    )
    prompt = (
        "<|system|>You are a medical translator. Translate the following Spanish medical "
        "conversation transcript to English accurately. Preserve all medical details, names, "
        "dosages, and diagnoses exactly.<|end|>\n"
        "<|user|>Translate this Spanish transcript to English:\n\n"
        f"{spanish_text}<|end|>\n"
        "<|assistant|>"
    )
    return model.generate_text(
        prompt=prompt,
        params={"max_new_tokens": 1500, "stop_sequences": ["<|user|>", "<|system|>"]}
    ).strip()


def transcribe_audio(audio_file_path: str, visit_id: str, content_type: str = None, language: str = 'en', environment: str = 'clinic') -> str:
    profile = AUDIO_PROFILES.get(environment, AUDIO_PROFILES['clinic'])
    print(f"Transcribing audio for visit {visit_id} (language: {language}, environment: {profile['label']})...")

    if content_type is None:
        ext = os.path.splitext(audio_file_path)[1].lower()
        content_type = {
            '.wav':  'audio/wav',
            '.webm': 'audio/webm',
            '.ogg':  'audio/ogg',
            '.mp3':  'audio/mp3',
            '.flac': 'audio/flac',
        }.get(ext, 'audio/wav')

    stt_model = 'es-ES_BroadbandModel' if language == 'es' else 'en-US_BroadbandModel'

    with open(audio_file_path, 'rb') as audio_file:
        result = stt.recognize(
            audio=audio_file,
            content_type=content_type,
            model=stt_model,
            smart_formatting=True,
            background_audio_suppression=profile['background_audio_suppression'],
            speech_detector_sensitivity=profile['speech_detector_sensitivity'],
        ).get_result()

    lines = []
    for item in result.get('results', []):
        lines.append(item['alternatives'][0]['transcript'].strip())
    transcript = ' '.join(lines)

    if not transcript.strip():
        raise ValueError('Watson STT returned an empty transcript. Check the audio quality and format.')

    transcript = clean_transcript(transcript)

    if not transcript.strip():
        raise ValueError('Transcript was empty after cleaning. The audio may have been too noisy or unclear.')

    # If Spanish audio, translate to English so the rest of the pipeline works normally
    if language == 'es':
        print(f"Translating Spanish transcript to English for visit {visit_id}...")
        transcript = _translate_to_english(transcript)

    # Save locally
    folder = f"patient_files/{visit_id}"
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/transcript.txt", "w") as f:
        f.write(transcript)

    print(f"Audio transcribed for visit {visit_id}")
    return transcript
