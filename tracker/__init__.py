"""Gmail internship application tracker.

Reads job-application emails from Gmail, classifies and summarizes them with
Gemini on Vertex AI (with a regex fallback), and keeps a Google Sheet up to date.
"""

__version__ = "2.0.0"
