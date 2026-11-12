from tracker.config import Settings
from tracker.pipeline import find_matching_application, normalize_company, role_similarity, run_sync
from tracker.schemas import ApplicationRow, Category
from tracker.sheets_io import DryRunStore, InMemoryStore

from .fakes import FakeClassifier, FakeGmail, analysis, gmail_message

SETTINGS = Settings(sheet_id="test", llm_max_workers=4)
quiet = lambda *_: None  # noqa: E731


def sync(messages, results, store=None, **kw):
    store = store if store is not None else InMemoryStore()
    clf = kw.pop("classifier", None) or FakeClassifier(results)
    res = run_sync(SETTINGS, FakeGmail(messages), store, clf, log=quiet, **kw)
    return res, store, clf


def test_confirmation_creates_row_and_marks_processed():
    msgs = [gmail_message("m1", "Thanks for applying to Acme", "We received your application.")]
    res, store, _ = sync(msgs, {"m1": analysis(Category.APPLICATION_CONFIRMATION, job_id="R-1", location="NYC", is_internship=True)})
    assert res.new_applications == 1 and res.classified == 1
    row = store.rows[0]
    assert (row.company, row.status, row.job_id, row.location, row.internship) == ("Acme", "Applied", "R-1", "NYC", "Yes")
    assert row.date_applied == "2025-10-09" and row.message_id == "m1"
    assert row.thread_url.endswith("thread-m1")
    assert store.processed == {"m1": "application_confirmation"}
    assert store.summary[1][:3] == ["Acme", 1, 1]


def test_later_rejection_updates_status_in_order():
    # Delivered out of order by the search; the pipeline must sort by date.
    msgs = [
        gmail_message("rej", "Update on your application", "Unfortunately...", day=10),
        gmail_message("conf", "Application received", "Thanks!", day=0),
    ]
    results = {
        "conf": analysis(Category.APPLICATION_CONFIRMATION, company="Acme Inc."),
        "rej": analysis(Category.REJECTION, company="ACME", summary="Acme declined."),
    }
    res, store, _ = sync(msgs, results)
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row.status == "Rejected" and row.summary == "Acme declined." and row.last_update == "2025-10-19"
    assert res.status_updates == 1 and res.new_applications == 1


def test_duplicate_confirmation_is_skipped():
    msgs = [gmail_message("li", "Your application was sent to Acme", "LinkedIn"),
            gmail_message("ats", "Thank you for applying", "Workday", day=1)]
    results = {"li": analysis(Category.APPLICATION_CONFIRMATION), "ats": analysis(Category.APPLICATION_CONFIRMATION)}
    res, store, _ = sync(msgs, results)
    assert len(store.rows) == 1 and res.duplicates == 1
    assert set(store.processed) == {"li", "ats"}


def test_two_roles_at_same_company_are_distinct():
    msgs = [gmail_message("a", "s", "b"), gmail_message("b", "s", "b", day=1)]
    results = {"a": analysis(Category.APPLICATION_CONFIRMATION, role="Data Science Intern"),
               "b": analysis(Category.APPLICATION_CONFIRMATION, role="Product Design Intern")}
    _, store, _ = sync(msgs, results)
    assert [r.role_title for r in store.rows] == ["Data Science Intern", "Product Design Intern"]


def test_not_job_related_is_ignored_but_processed():
    msgs = [gmail_message("n", "Jobs you may like", "Recommended jobs")]
    res, store, _ = sync(msgs, {"n": analysis(Category.NOT_JOB_RELATED, company=None, role=None)})
    assert store.rows == [] and res.ignored == 1 and store.processed == {"n": "not_job_related"}


def test_already_processed_messages_are_not_reclassified():
    msgs = [gmail_message("old", "s", "b"), gmail_message("new", "s", "b")]
    store = InMemoryStore(processed={"old"})
    res, _, clf = sync(msgs, {"new": analysis(Category.APPLICATION_CONFIRMATION)}, store=store)
    assert clf.seen == ["new"] and res.already_processed == 1


def test_classifier_errors_are_retried_next_run():
    msgs = [gmail_message("bad", "s", "b"), gmail_message("ok", "s", "b")]
    clf = FakeClassifier({"ok": analysis(Category.APPLICATION_CONFIRMATION)}, fail={"bad"})
    res, store, _ = sync(msgs, {}, classifier=clf)
    assert len(res.errors) == 1 and "bad" in res.errors[0]
    assert "bad" not in store.processed and "ok" in store.processed


def test_status_email_without_confirmation_creates_row():
    msgs = [gmail_message("iv", "Interview invitation", "Pick a time")]
    res, store, _ = sync(msgs, {"iv": analysis(Category.INTERVIEW, company="Globex")})
    assert store.rows[0].status == "Interview" and store.rows[0].date_applied == ""
    assert res.new_applications == 1


def test_older_status_email_does_not_override_newer():
    store = InMemoryStore([ApplicationRow(company="Acme", role_title="SWE Intern", status="Interview",
                                          last_update="2025-12-01", date_applied="2025-10-01")])
    msgs = [gmail_message("x", "s", "b", day=5)]  # 2025-10-14, older than last_update
    res, store, _ = sync(msgs, {"x": analysis(Category.STATUS_UPDATE, role="SWE Intern")}, store=store)
    assert store.rows[0].status == "Interview" and res.status_updates == 0


def test_dry_run_writes_nothing():
    base = InMemoryStore()
    dry = DryRunStore(base)
    msgs = [gmail_message("m1", "s", "b")]
    res, _, _ = sync(msgs, {"m1": analysis(Category.APPLICATION_CONFIRMATION)}, store=dry, dry_run=True)
    assert base.rows == [] and base.processed == {}
    assert len(dry.appended) == 1 and dry.marked == [("m1", "application_confirmation")]
    assert res.dry_run and "[DRY RUN]" in res.report()


def test_backfill_summarizes_legacy_rows():
    legacy = ApplicationRow(company="Initech", role_title="", date_applied="2025-03-01", message_id="leg")
    store = InMemoryStore([legacy], processed={"leg"})
    msgs = [gmail_message("leg", "Application received", "Thanks for applying to the QA Intern role")]
    clf = FakeClassifier({"leg": analysis(Category.APPLICATION_CONFIRMATION, company="Initech", role="QA Intern",
                                          summary="Initech confirmed your QA Intern application.")})
    res, store, _ = sync(msgs, {}, store=store, classifier=clf, backfill=True)
    row = store.rows[0]
    assert res.backfilled == 1
    assert (row.summary, row.status, row.role_title, row.classified_by) == (
        "Initech confirmed your QA Intern application.", "Applied", "QA Intern", "fake-gemini")


def test_backfill_flags_false_positives_for_review():
    store = InMemoryStore([ApplicationRow(company="Spam Co", message_id="fp")], processed={"fp"})
    clf = FakeClassifier({"fp": analysis(Category.NOT_JOB_RELATED, company=None, role=None)})
    sync([gmail_message("fp", "Newsletter", "...")], {}, store=store, classifier=clf, backfill=True)
    assert store.rows[0].status == "Needs Review" and store.rows[0].company == "Spam Co"


def test_company_normalization_and_matching():
    assert normalize_company("Acme, Inc.") == normalize_company("ACME") == "acme"
    assert normalize_company("Procter & Gamble") == "procter and gamble"
    assert role_similarity("Software Engineering Intern", "Software Engineer Intern - Summer 2026") > 0.2
    rows = [ApplicationRow(company="Acme", role_title="Data Intern", job_id="R-9"),
            ApplicationRow(company="Acme", role_title="SWE Intern", job_id="R-7")]
    assert find_matching_application(rows, analysis(Category.REJECTION, job_id="r-7", role=None)).role_title == "SWE Intern"
    assert find_matching_application(rows, analysis(Category.REJECTION, role="Data Intern")).job_id == "R-9"
    assert find_matching_application(rows, analysis(Category.REJECTION, company="Other")) is None
