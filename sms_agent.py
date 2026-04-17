import os
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

client = Client(os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN"))

def send_patient_sms(patient_phone: str, summary_text: str, visit_id: str):
    # Extract just the patient summary section
    if "PATIENT SUMMARY:" in summary_text:
        start = summary_text.index("PATIENT SUMMARY:") + len("PATIENT SUMMARY:")
        patient_part = summary_text[start:]
        if "CLINICAL SUMMARY:" in patient_part:
            patient_part = patient_part[:patient_part.index("CLINICAL SUMMARY:")]
        patient_part = patient_part.strip()
    else:
        patient_part = summary_text.strip()

    # Keep under 300 chars so it fits in 2 SMS segments
    if len(patient_part) > 280:
        patient_part = patient_part[:277] + '...'

    body = f"IntelliCare visit summary:\n\n{patient_part}\n\nReply HELP for questions."

    message = client.messages.create(
        body=body,
        from_=os.getenv("TWILIO_FROM"),
        to=patient_phone
    )

    print(f"SMS sent to {patient_phone} | SID: {message.sid}")
    return message.sid
