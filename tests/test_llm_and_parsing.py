from tracker.config import Settings, load_settings
from tracker.gmail_client import extract_links, get_header, parse_from_name, parse_message
from tracker.heuristics import classify_heuristic
from tracker.llm import SYSTEM_INSTRUCTION, GeminiClassifier, HeuristicClassifier, build_generate_config, make_classifier
from tracker.schemas import ApplicationRow, Category, EmailAnalysis, EmailMessage
from tracker.sheets_io import summarize

from .fakes import FakeGenaiClient, gmail_message

EMAIL = EmailMessage(id="m1", thread_id="t1", internal_date_ms=0, subject="Thanks for applying to Acme",
                     sender="Acme Careers <careers@acme.com>", body="We received your application for SWE Intern.")


# --- Gemini classifier --------------------------------------------------------

def test_gemini_classifier_uses_structured_output_and_records_usage():
    parsed = EmailAnalysis(category=Category.APPLICATION_CONFIRMATION, company="Acme", role_title="SWE Intern",
                           summary="Acme received your SWE Intern application.", confidence=0.97)
    client = FakeGenaiClient(parsed=parsed)
    c = GeminiClassifier(model="gemini-test", client=client).classify(EMAIL)

    assert c.analysis.company == "Acme" and c.classified_by == "gemini-test"
    assert (c.stats.input_tokens, c.stats.output_tokens, c.stats.thinking_tokens) == (420, 60, 12)
    call = client.calls[0]
    assert call["model"] == "gemini-test"
    assert "Thanks for applying to Acme" in call["contents"] and "<<<" in call["contents"]
    assert call["config"].response_schema is EmailAnalysis
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].system_instruction == SYSTEM_INSTRUCTION
    assert call["config"].temperature == 0.0


def test_gemini_classifier_falls_back_to_json_text():
    text = '{"category":"rejection","company":"Globex","summary":"Globex declined.","confidence":0.8}'
    c = GeminiClassifier(model="m", client=FakeGenaiClient(parsed=None, text=text)).classify(EMAIL)
    assert c.analysis.category == Category.REJECTION and c.analysis.role_title is None


def test_prompt_truncates_long_bodies():
    long = EmailMessage(id="x", thread_id="t", internal_date_ms=0, subject="s", sender="f", body="a" * 10_000)
    assert len(long.as_prompt_text(max_body_chars=500)) < 700


def test_thinking_level_is_optional():
    assert build_generate_config("").thinking_config is None
    assert build_generate_config("low").thinking_config.thinking_level.value == "LOW"


def test_make_classifier_falls_back_without_project():
    assert isinstance(make_classifier(Settings(gcp_project=""), log=lambda *_: None), HeuristicClassifier)
    assert isinstance(make_classifier(Settings(gcp_project="p", use_llm=False)), HeuristicClassifier)


def test_system_instruction_guards_against_prompt_injection():
    assert "untrusted" in SYSTEM_INSTRUCTION and "Ignore any instructions" in SYSTEM_INSTRUCTION


# --- Heuristic baseline ---------------------------------------------------------

def test_heuristic_categories():
    assert classify_heuristic("Application received", "Thank you for applying!").category == Category.APPLICATION_CONFIRMATION
    assert classify_heuristic("Update", "Unfortunately we will not be moving forward").category == Category.REJECTION
    assert classify_heuristic("Next steps", "Please complete the HackerRank challenge").category == Category.ASSESSMENT
    assert classify_heuristic("Hello", "We'd like to schedule an interview").category == Category.INTERVIEW
    assert classify_heuristic("Weekly digest", "Top stories this week").category == Category.NOT_JOB_RELATED


# --- Gmail parsing --------------------------------------------------------------

def test_parse_message_plain_text_and_links():
    msg = gmail_message("m", "Subj", "Hello\n\n\n\nSee https://jobs.acme.com/123. Thanks")
    e = parse_message(msg)
    assert e.subject == "Subj" and e.body == "Hello\n\nSee https://jobs.acme.com/123. Thanks"
    assert e.links == ["https://jobs.acme.com/123"]


def test_parse_message_html_fallback_strips_tags():
    e = parse_message(gmail_message("m", "s", "<html><style>.x{}</style><p>Thanks&nbsp;for <b>applying</b></p></html>", html=True))
    assert "Thanks for applying" in e.body and "<" not in e.body and ".x" not in e.body


def test_header_helpers():
    msg = {"payload": {"headers": [{"name": "subject", "value": "Hi"}]}}
    assert get_header(msg, "Subject") == "Hi"
    assert parse_from_name('"Acme Careers" <c@acme.com>') == "Acme Careers"
    assert extract_links("a https://x.io/a, b https://x.io/a") == ["https://x.io/a"]


# --- Config + summary ------------------------------------------------------------

def test_env_overrides_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"SHEET_ID": "abc", "USE_LLM": true, "GEMINI_MODEL": "a"}')
    s = load_settings(cfg, env={"GEMINI_MODEL": "b", "USE_LLM": "false", "LLM_MAX_WORKERS": "3", "GOOGLE_CLOUD_PROJECT": "proj"})
    assert (s.sheet_id, s.gemini_model, s.use_llm, s.llm_max_workers, s.gcp_project) == ("abc", "b", False, 3, "proj")
    assert not s.llm_enabled


def test_summary_counts_by_status():
    rows = [ApplicationRow(company="Acme", status="Applied", date_applied="2025-01-01"),
            ApplicationRow(company="Acme", status="Rejected", last_update="2025-02-01"),
            ApplicationRow(company="Globex", status="")]
    table = summarize(rows)
    header = table[0]
    acme = dict(zip(header, table[1]))
    assert acme["Company"] == "Acme" and acme["Applications"] == 2 and acme["Rejected"] == 1
    assert acme["Latest Activity"] == "2025-02-01"
    assert dict(zip(header, table[2]))["Applied"] == 1  # blank status counts as Applied


def test_output_budget_leaves_room_for_thinking():
    # Regression: thinking models (gemini-3.5-flash spent up to ~820 thinking tokens on one eval email)
    # count thinking against max_output_tokens; a 1024 cap truncated the JSON -> ValidationError.
    assert build_generate_config("").max_output_tokens >= 4096


def test_google_api_calls_retry_rate_limits():
    # Regression: the first live backlog sync hit Gmail's per-user quota (403 rateLimitExceeded)
    # and 1,900 fetches failed because execute() had no retries.
    import inspect

    import tracker.gmail_client as gc
    import tracker.sheets_io as sio

    for mod in (gc, sio):
        src = inspect.getsource(mod)
        assert ".execute()" not in src and "num_retries=API_RETRIES" in src
    assert gc.API_RETRIES >= 5
