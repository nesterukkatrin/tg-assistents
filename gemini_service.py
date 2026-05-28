import asyncio
import json
import re

import google.generativeai as genai

_model = None

EXTRACT_PROMPT = """Transcribe this voice message and extract all tasks from it.
The message may be in Ukrainian, Russian, English, or a mix of languages.

Return ONLY valid JSON, no extra text:
{
  "transcript": "full transcription preserving original language",
  "tasks": [
    {
      "title": "short task title in original language",
      "description": "detailed description if any, or null",
      "responsible": "person responsible or null if not mentioned",
      "deadline": "deadline as mentioned (e.g. 'до п'ятниці', 'by June 5') or null",
      "priority": "high or medium or low"
    }
  ]
}

Rules:
- Extract ALL tasks mentioned
- If priority is not mentioned → use "medium"
- If responsible is not mentioned → use null
- If deadline is not mentioned → use null
- Preserve original language for title and description
"""

CLARIFY_PROMPT = """You previously extracted tasks from a voice message transcript.
The user wants to correct or clarify something.

Original transcript:
{transcript}

Current extracted tasks:
{tasks}

User clarification:
{clarification}

Apply the clarification and return updated tasks as ONLY valid JSON, no extra text:
{{
  "tasks": [
    {{
      "title": "...",
      "description": "... or null",
      "responsible": "... or null",
      "deadline": "... or null",
      "priority": "high or medium or low"
    }}
  ]
}}
"""


def init_gemini(api_key: str):
    global _model
    genai.configure(api_key=api_key)
    _model = genai.GenerativeModel("gemini-1.5-flash")


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def _process_voice_sync(audio_path: str) -> dict:
    uploaded = genai.upload_file(audio_path)
    try:
        response = _model.generate_content([uploaded, EXTRACT_PROMPT])
        return _parse_json(response.text)
    finally:
        uploaded.delete()


def _reprocess_sync(transcript: str, current_tasks: list, clarification: str) -> dict:
    prompt = CLARIFY_PROMPT.format(
        transcript=transcript,
        tasks=json.dumps(current_tasks, ensure_ascii=False, indent=2),
        clarification=clarification,
    )
    response = _model.generate_content(prompt)
    return _parse_json(response.text)


async def process_voice_message(audio_path: str) -> dict:
    return await asyncio.to_thread(_process_voice_sync, audio_path)


async def reprocess_with_clarification(
    transcript: str, current_tasks: list, clarification: str
) -> dict:
    return await asyncio.to_thread(_reprocess_sync, transcript, current_tasks, clarification)
