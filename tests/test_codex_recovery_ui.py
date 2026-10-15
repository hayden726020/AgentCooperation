"""Behavioral tests for the Codex recovery-time UI and its API.

Backend tests run against real temporary SQLite databases wired through
``web.api._core_scope`` so no production ledger is touched. Frontend tests run
the real ``web/static/js/codex_recovery.js`` module inside a Node.js ``vm``
sandbox with a minimal fake DOM/fetch, exercising concurrency and mode
isolation rather than only pure helpers.
"""

import json
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, select

from storage.db import get_engine, init_db
from storage.models import CodexQuotaRecord
from web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_JS = ROOT / "web" / "static" / "js" / "codex_recovery.js"
NODE = shutil.which("node")

UTC = timezone.utc


def _aware(value):
    """Normalize a stored datetime to aware UTC for comparison.

    SQLite round-trips may return naive values; the project stores UTC, so a
    naive value is interpreted as UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _FakeGuard:
    """Minimal CodexGuard stand-in exposing the interface the API relies on."""

    def __init__(self, engine):
        self._engine = engine

    def set_quota_state(self, state, reason=None, recovery_at=None):
        with Session(self._engine) as session:
            record = session.exec(
                select(CodexQuotaRecord).order_by(CodexQuotaRecord.id.desc())
            ).first()
            if record is None:
                record = CodexQuotaRecord()
            record.state = state
            record.exhausted_reason = reason
            record.estimated_recovery_at = recovery_at
            session.add(record)
            session.commit()


class _FakeCore:
    """Context-manager core scope backed by a real temporary SQLite engine."""

    def __init__(self, engine):
        self._engine = engine
        self.codex_guard = _FakeGuard(engine)

    def _session(self):
        return Session(self._engine)


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    """Point mock and live ledgers at two distinct temporary databases."""
    mock_engine = get_engine(str(tmp_path / "mock.db"))
    live_engine = get_engine(str(tmp_path / "live.db"))
    init_db(mock_engine)
    init_db(live_engine)

    @contextmanager
    def fake_core_scope(mock=False):
        engine = mock_engine if mock else live_engine
        yield _FakeCore(engine)

    import web.api as api_module

    monkeypatch.setattr(api_module, "_core_scope", fake_core_scope)
    return {"mock": mock_engine, "live": live_engine}


@pytest.fixture
def client(ledgers):
    app = create_app(mock_default=True)
    app.config["TESTING"] = True
    return app.test_client()


def _seed(engine, *, state="EXHAUSTED", reason="weekly quota", recovery=None):
    with Session(engine) as session:
        record = CodexQuotaRecord(
            state=state, exhausted_reason=reason, estimated_recovery_at=recovery
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def _read(engine):
    with Session(engine) as session:
        return session.exec(
            select(CodexQuotaRecord).order_by(CodexQuotaRecord.id.desc())
        ).first()


# --------------------------------------------------------------------------
# Backend: POST /api/codex/recovery
# --------------------------------------------------------------------------

def test_save_with_offset_roundtrips_beijing(client, ledgers):
    _seed(ledgers["live"])
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": False, "estimated_recovery_at": "2026-10-20T12:00:00+08:00"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["mock_mode"] is False
    assert data["estimated_recovery_at"] == "2026-10-20T12:00:00+08:00"

    stored = _read(ledgers["live"])
    assert _aware(stored.estimated_recovery_at) == datetime(2026, 10, 20, 4, 0, 0, tzinfo=UTC)

    # GET must render the same Beijing wall clock.
    got = client.get("/api/codex/quota?mock=false").get_json()
    assert got["quota"]["estimated_recovery_at"] == "2026-10-20T12:00:00+08:00"


def test_save_naive_input_is_beijing(client, ledgers):
    _seed(ledgers["live"])
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": False, "estimated_recovery_at": "2026-10-20T12:00:00"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["estimated_recovery_at"] == "2026-10-20T12:00:00+08:00"
    assert _aware(_read(ledgers["live"]).estimated_recovery_at) == datetime(
        2026, 10, 20, 4, 0, 0, tzinfo=UTC
    )


def test_clear_null(client, ledgers):
    _seed(ledgers["live"], recovery=datetime(2026, 10, 20, 4, 0, 0, tzinfo=UTC))
    resp = client.post(
        "/api/codex/recovery", json={"mock": False, "estimated_recovery_at": None}
    )
    assert resp.status_code == 200
    assert resp.get_json()["estimated_recovery_at"] is None
    assert _read(ledgers["live"]).estimated_recovery_at is None
    got = client.get("/api/codex/quota?mock=false").get_json()
    assert got["quota"]["estimated_recovery_at"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"mock": False, "estimated_recovery_at": "not-a-date"},
        {"mock": False, "estimated_recovery_at": "2026-02-30T10:00:00"},
        {"mock": False, "estimated_recovery_at": ""},
        {"mock": False, "estimated_recovery_at": 12345},
        {"mock": False, "estimated_recovery_at": ["2026-10-20T12:00:00+08:00"]},
        {"mock": False},
    ],
)
def test_invalid_date_or_type_returns_400_without_mutation(client, ledgers, payload):
    _seed(ledgers["live"], recovery=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    resp = client.post("/api/codex/recovery", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert _aware(_read(ledgers["live"]).estimated_recovery_at) == datetime(
        2026, 1, 1, 0, 0, 0, tzinfo=UTC
    )


def test_overflow_date_returns_400_without_mutation(client, ledgers):
    _seed(ledgers["live"], recovery=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": False, "estimated_recovery_at": "9999-12-31T23:59:59+14:00"},
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert _aware(_read(ledgers["live"]).estimated_recovery_at) == datetime(
        2026, 1, 1, 0, 0, 0, tzinfo=UTC
    )


def test_non_object_body_returns_400(client, ledgers):
    _seed(ledgers["live"])
    resp = client.post("/api/codex/recovery", json=["nope"])
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_invalid_mock_type_returns_400(client, ledgers):
    _seed(ledgers["live"])
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": "yes", "estimated_recovery_at": "2026-10-20T12:00:00+08:00"},
    )
    assert resp.status_code == 400
    assert _read(ledgers["live"]).estimated_recovery_at is None


def test_string_false_mock_does_not_select_wrong_ledger(client, ledgers):
    _seed(ledgers["live"], state="EXHAUSTED", reason="live reason")
    _seed(ledgers["mock"], state="READY", reason=None)
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": "false", "estimated_recovery_at": "2026-10-20T12:00:00+08:00"},
    )
    assert resp.status_code == 400
    assert _read(ledgers["live"]).estimated_recovery_at is None
    assert _read(ledgers["mock"]).estimated_recovery_at is None


def test_state_and_reason_preserved(client, ledgers):
    _seed(ledgers["live"], state="EXHAUSTED", reason="weekly quota")
    resp = client.post(
        "/api/codex/recovery",
        json={"mock": False, "estimated_recovery_at": "2026-10-20T12:00:00+08:00"},
    )
    data = resp.get_json()
    assert data["state"] == "EXHAUSTED"
    assert data["exhausted_reason"] == "weekly quota"
    stored = _read(ledgers["live"])
    assert stored.state == "EXHAUSTED"
    assert stored.exhausted_reason == "weekly quota"


def test_mock_live_isolation(client, ledgers):
    _seed(ledgers["live"], state="EXHAUSTED", reason="live reason")
    _seed(ledgers["mock"], state="READY", reason=None)

    client.post(
        "/api/codex/recovery",
        json={"mock": True, "estimated_recovery_at": "2026-11-01T09:30:00+08:00"},
    )
    assert _aware(_read(ledgers["mock"]).estimated_recovery_at) == datetime(
        2026, 11, 1, 1, 30, 0, tzinfo=UTC
    )
    assert _read(ledgers["live"]).estimated_recovery_at is None

    client.post(
        "/api/codex/recovery",
        json={"mock": False, "estimated_recovery_at": "2026-12-25T00:00:00+08:00"},
    )
    assert _aware(_read(ledgers["live"]).estimated_recovery_at) == datetime(
        2026, 12, 24, 16, 0, 0, tzinfo=UTC
    )
    assert _aware(_read(ledgers["mock"]).estimated_recovery_at) == datetime(
        2026, 11, 1, 1, 30, 0, tzinfo=UTC
    )


def test_legacy_quota_endpoint_still_works(client, ledgers):
    _seed(ledgers["live"])
    resp = client.post(
        "/api/codex/quota",
        json={"state": "RECOVERING", "mock": False, "reason": "operator"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["state"] == "RECOVERING"
    assert _read(ledgers["live"]).state == "RECOVERING"


# --------------------------------------------------------------------------
# Frontend: run the real module in Node.js vm with a fake DOM/fetch
# --------------------------------------------------------------------------

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const modulePath = process.argv[2];
const scenario = process.argv[3];
const source = fs.readFileSync(modulePath, 'utf8');

function makeEl(id) {
  const el = {
    id, value: '', textContent: '', className: '', disabled: false,
    _listeners: {}, _attrs: {},
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    dispatch(type, event) { (this._listeners[type] || []).forEach(fn => fn(event || {})); },
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return this._attrs[k]; },
    classList: { add() {}, remove() {} },
  };
  return el;
}

const els = {
  codexRecoveryForm: makeEl('codexRecoveryForm'),
  codexRecoveryInput: makeEl('codexRecoveryInput'),
  codexRecoverySaveBtn: makeEl('codexRecoverySaveBtn'),
  codexRecoveryClearBtn: makeEl('codexRecoveryClearBtn'),
  codexRecoveryCurrent: makeEl('codexRecoveryCurrent'),
  codexRecoveryFeedback: makeEl('codexRecoveryFeedback'),
  codexRecoveryHint: makeEl('codexRecoveryHint'),
};

const calls = [];
let fetchImpl = null;

const sandbox = {
  console,
  setTimeout, clearTimeout,
  Date, JSON, Math, Number, String, Object, Array, Error, Promise, RegExp,
  document: {
    getElementById: (id) => els[id] || null,
    activeElement: null,
  },
  fetch: (...args) => {
    calls.push(args);
    return fetchImpl(...args);
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: modulePath });
const CR = sandbox.CodexRecovery;

function jsonResponse(body, status) {
  return Promise.resolve({
    ok: (status || 200) < 400,
    status: status || 200,
    json: () => Promise.resolve(body),
  });
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const results = {};

async function run() {
  if (scenario === 'time_conversion') {
    results.iso = CR.localInputToBeijingIso('2026-10-20T12:00');
    results.isoWithSeconds = CR.localInputToBeijingIso('2026-10-20T12:00:30');
    results.input = CR.isoToLocalInput('2026-10-20T12:00:00+08:00');
    results.display = CR.formatBeijingDisplay('2026-10-20T12:00:00+08:00');
    results.utcInput = CR.localInputToBeijingIso('2026-10-20T04:00');
    let threw = false;
    try { CR.localInputToBeijingIso('2026-02-30T10:00'); } catch (e) { threw = true; }
    results.invalidThrew = threw;
    let emptyThrew = false;
    try { CR.localInputToBeijingIso(''); } catch (e) { emptyThrew = true; }
    results.emptyThrew = emptyThrew;
  } else if (scenario === 'load_failure') {
    CR.init();
    fetchImpl = () => jsonResponse({ error: 'boom' }, 500);
    await CR.refresh(false, false);
    results.feedback = els.codexRecoveryFeedback.textContent;
    results.inputValue = els.codexRecoveryInput.value;
    results.saveDisabled = els.codexRecoverySaveBtn.disabled;
    results.busy = CR.state.saving;
  } else if (scenario === 'edit_guard') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-11-11T11:11';
    els.codexRecoveryInput.dispatch('input', {});
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-12-31T23:59:00+08:00' } });
    await CR.refresh(false, true);
    results.inputValue = els.codexRecoveryInput.value;
    results.savedIso = CR.state.savedIso;
    results.dirty = CR.state.dirty;
  } else if (scenario === 'edit_guard_background_failure') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-11-11T11:11';
    els.codexRecoveryInput.dispatch('input', {});
    fetchImpl = () => jsonResponse({ error: 'boom' }, 500);
    await CR.refresh(false, true);
    results.inputValue = els.codexRecoveryInput.value;
    results.dirty = CR.state.dirty;
    results.savedIso = CR.state.savedIso;
    results.feedback = els.codexRecoveryFeedback.textContent;
  } else if (scenario === 'mode_isolation') {
    CR.init();
    const first = deferred();
    fetchImpl = () => first.promise;
    const p1 = CR.refresh(false, false);
    CR.onModeChange();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-11-01T09:00:00+08:00' } });
    await CR.refresh(true, false);
    first.resolve({ ok: true, status: 200, json: () => Promise.resolve({ quota: { estimated_recovery_at: '2026-01-01T00:00:00+08:00' } }) });
    await p1;
    results.savedIso = CR.state.savedIso;
    results.inputValue = els.codexRecoveryInput.value;
    results.loadedMode = CR.state.loadedMode;
  } else if (scenario === 'save_refresh_race') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-10-21T12:00';
    const saveDeferred = deferred();
    fetchImpl = (url, opts) => {
      if (opts && opts.method === 'POST') return saveDeferred.promise;
      return jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    };
    const savePromise = CR.save();
    await CR.refresh(false, true);
    results.busyDuringSave = CR.state.saving;
    saveDeferred.resolve({ ok: true, status: 200, json: () => Promise.resolve({ success: true, estimated_recovery_at: '2026-10-21T12:00:00+08:00' }) });
    await savePromise;
    results.busyAfterSave = CR.state.saving;
    results.saveDisabled = els.codexRecoverySaveBtn.disabled;
    results.feedback = els.codexRecoveryFeedback.textContent;
    results.savedIso = CR.state.savedIso;
  } else if (scenario === 'save_refresh_race_stale_read') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-10-21T12:00';
    const staleRead = deferred();
    const saveDeferred = deferred();
    let readIssued = false;
    fetchImpl = (url, opts) => {
      if (opts && opts.method === 'POST') return saveDeferred.promise;
      if (!readIssued) { readIssued = true; return staleRead.promise; }
      return jsonResponse({ quota: { estimated_recovery_at: '2026-10-21T12:00:00+08:00' } });
    };
    const savePromise = CR.save();
    const staleReadPromise = CR.refresh(false, true);
    saveDeferred.resolve({ ok: true, status: 200, json: () => Promise.resolve({ success: true, estimated_recovery_at: '2026-10-21T12:00:00+08:00' }) });
    await savePromise;
    staleRead.resolve({ ok: true, status: 200, json: () => Promise.resolve({ quota: { estimated_recovery_at: null } }) });
    await staleReadPromise;
    results.savedIso = CR.state.savedIso;
    results.inputValue = els.codexRecoveryInput.value;
    results.currentText = els.codexRecoveryCurrent.textContent;
  } else if (scenario === 'ready_gate') {
    CR.init();
    results.initialSaveDisabled = els.codexRecoverySaveBtn.disabled;
    results.initialClearDisabled = els.codexRecoveryClearBtn.disabled;
    results.initialInputDisabled = els.codexRecoveryInput.disabled;
    const pending = deferred();
    fetchImpl = () => pending.promise;
    const p = CR.refresh(false, false);
    results.loadingSaveDisabled = els.codexRecoverySaveBtn.disabled;
    results.loadingClearDisabled = els.codexRecoveryClearBtn.disabled;
    pending.resolve({ ok: true, status: 200, json: () => Promise.resolve({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } }) });
    await p;
    results.readySaveDisabled = els.codexRecoverySaveBtn.disabled;
    results.readyClearDisabled = els.codexRecoveryClearBtn.disabled;
    results.readyInputDisabled = els.codexRecoveryInput.disabled;
    results.loadedMode = CR.state.loadedMode;
  } else if (scenario === 'ready_gate_failure') {
    CR.init();
    fetchImpl = () => jsonResponse({ error: 'boom' }, 500);
    await CR.refresh(false, false);
    results.saveDisabled = els.codexRecoverySaveBtn.disabled;
    results.clearDisabled = els.codexRecoveryClearBtn.disabled;
    results.inputDisabled = els.codexRecoveryInput.disabled;
    results.loadedMode = CR.state.loadedMode;
    results.feedback = els.codexRecoveryFeedback.textContent;
  } else if (scenario === 'clear_before_ready') {
    CR.init();
    const pending = deferred();
    fetchImpl = () => pending.promise;
    const p = CR.refresh(false, false);
    await CR.clear();
    results.postCount = calls.filter(c => c[1] && c[1].method === 'POST').length;
    pending.resolve({ ok: true, status: 200, json: () => Promise.resolve({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } }) });
    await p;
  } else if (scenario === 'enter_submit') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-10-22T12:00';
    let prevented = false;
    els.codexRecoveryForm.dispatch('submit', { preventDefault: () => { prevented = true; } });
    await new Promise(r => setTimeout(r, 20));
    results.prevented = prevented;
    results.postCount = calls.filter(c => c[1] && c[1].method === 'POST').length;
    results.feedback = els.codexRecoveryFeedback.textContent;
  } else if (scenario === 'switch_back_stale') {
    CR.init();
    const liveDeferred = deferred();
    fetchImpl = () => liveDeferred.promise;
    const livePromise = CR.refresh(false, false);
    CR.onModeChange();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-11-05T08:00:00+08:00' } });
    await CR.refresh(true, false);
    CR.onModeChange();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-11-06T08:00:00+08:00' } });
    await CR.refresh(false, false);
    liveDeferred.resolve({ ok: true, status: 200, json: () => Promise.resolve({ quota: { estimated_recovery_at: '2026-01-01T00:00:00+08:00' } }) });
    await livePromise;
    results.savedIso = CR.state.savedIso;
    results.inputValue = els.codexRecoveryInput.value;
  } else if (scenario === 'mode_switch_clears_draft') {
    CR.init();
    fetchImpl = () => jsonResponse({ quota: { estimated_recovery_at: '2026-10-20T12:00:00+08:00' } });
    await CR.refresh(false, false);
    els.codexRecoveryInput.value = '2026-11-11T11:11';
    els.codexRecoveryInput.dispatch('input', {});
    CR.onModeChange();
    results.inputValue = els.codexRecoveryInput.value;
    results.dirty = CR.state.dirty;
    results.savedIso = CR.state.savedIso;
    results.loadedMode = CR.state.loadedMode;
  } else if (scenario === 'app_wiring') {
    const appSource = fs.readFileSync(process.argv[4], 'utf8');
    const appEls = {
      mockModeToggle: makeEl('mockModeToggle'),
      autoRefreshToggle: makeEl('autoRefreshToggle'),
      envModeBadge: makeEl('envModeBadge'),
      headerSpend: makeEl('headerSpend'),
      headerQuotaBadge: makeEl('headerQuotaBadge'),
      codexCardState: makeEl('codexCardState'),
      codexCardLastCheck: makeEl('codexCardLastCheck'),
      codexCardRecovery: makeEl('codexCardRecovery'),
      codexCardReason: makeEl('codexCardReason'),
      auditsTableBody: makeEl('auditsTableBody'),
      tabCodex: makeEl('tab-codex'),
    };
    const appSandbox = {
      console, setTimeout, clearTimeout,
      Date, JSON, Math, Number, String, Object, Array, Error, Promise, RegExp,
      setInterval: () => 0, clearInterval: () => {},
      document: {
        getElementById: (id) => els[id] || appEls[id] || null,
        querySelectorAll: () => [],
        activeElement: null,
      },
      fetch: (...args) => { calls.push(args); return fetchImpl(...args); },
    };
    appSandbox.window = appSandbox;
    appSandbox.globalThis = appSandbox;
    vm.createContext(appSandbox);
    vm.runInContext(source, appSandbox, { filename: modulePath });
    vm.runInContext(appSource, appSandbox, { filename: process.argv[4] });
    const App = appSandbox.App;
    const CR2 = appSandbox.CodexRecovery;
    fetchImpl = (url) => {
      if (url.indexOf('/api/status') === 0) {
        return jsonResponse({
          spending: { total_cost_cny: 1.0, monthly_limit_cny: 200, warning_cny: 160, percent_used: 0.5, total_tokens: 0, prompt_tokens: 0, completion_tokens: 0 },
          codex_quota: { state: 'EXHAUSTED', estimated_recovery_at: '2026-10-20T12:00:00+08:00' },
          audits: { pending_count: 0 },
          tasks_count: { total: 0, success: 0, failed: 0, active: 0 },
        });
      }
      if (url.indexOf('/api/codex/quota') === 0) {
        return jsonResponse({ quota: { state: 'EXHAUSTED', estimated_recovery_at: '2026-10-20T12:00:00+08:00', exhausted_reason: 'weekly' }, audits: [] });
      }
      return jsonResponse({});
    };
    App.state.currentTab = 'codex';
    App.init(false);
    await new Promise(r => setTimeout(r, 30));
    results.headerRecovery = appEls.codexCardRecovery.textContent;
    results.formCurrent = els.codexRecoveryCurrent.textContent;
    results.loadedMode = CR2.state.loadedMode;
    results.quotaCalls = calls.filter(c => String(c[0]).indexOf('/api/codex/quota') === 0).length;
    results.statusCalls = calls.filter(c => String(c[0]).indexOf('/api/status') === 0).length;
  }

  process.stdout.write(JSON.stringify(results));
}

run().catch(err => {
  process.stdout.write(JSON.stringify({ error: String(err && err.stack || err) }));
  process.exitCode = 1;
});
"""


def _run_node(scenario, extra_args=None):
    if NODE is None:
        pytest.skip("Node.js is required for frontend behavior tests")
    harness = ROOT / ".pytest_temp" / "codex_recovery_harness.js"
    harness.parent.mkdir(exist_ok=True)
    harness.write_text(_HARNESS, encoding="utf-8")
    args = [NODE, str(harness), str(RECOVERY_JS), scenario]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.run(
        args,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


def test_frontend_time_conversion():
    out = _run_node("time_conversion")
    assert out["iso"] == "2026-10-20T12:00:00+08:00"
    assert out["isoWithSeconds"] == "2026-10-20T12:00:30+08:00"
    assert out["input"] == "2026-10-20T12:00"
    assert "北京时间" in out["display"]
    assert out["utcInput"] == "2026-10-20T04:00:00+08:00"
    assert out["invalidThrew"] is True
    assert out["emptyThrew"] is True


def test_frontend_load_failure_clears_form():
    out = _run_node("load_failure")
    assert "加载" in out["feedback"] or "失败" in out["feedback"]
    assert out["inputValue"] == ""
    assert out["saveDisabled"] is True
    assert out["busy"] is False


def test_frontend_edit_guard():
    out = _run_node("edit_guard")
    assert out["inputValue"] == "2026-11-11T11:11"
    assert out["savedIso"] == "2026-12-31T23:59:00+08:00"
    assert out["dirty"] is True


def test_frontend_edit_guard_background_failure_keeps_draft():
    out = _run_node("edit_guard_background_failure")
    assert out["inputValue"] == "2026-11-11T11:11"
    assert out["dirty"] is True
    assert out["savedIso"] == "2026-10-20T12:00:00+08:00"
    assert out["feedback"] != ""


def test_frontend_mode_isolation():
    out = _run_node("mode_isolation")
    assert out["savedIso"] == "2026-11-01T09:00:00+08:00"
    assert out["inputValue"] == "2026-11-01T09:00"
    assert out["loadedMode"] is True


def test_frontend_save_refresh_race():
    out = _run_node("save_refresh_race")
    assert out["busyDuringSave"] is True
    assert out["busyAfterSave"] is False
    assert out["saveDisabled"] is False
    assert out["savedIso"] == "2026-10-21T12:00:00+08:00"
    assert "保存" in out["feedback"] or "已" in out["feedback"]


def test_frontend_save_refresh_race_stale_read_discarded():
    out = _run_node("save_refresh_race_stale_read")
    assert out["savedIso"] == "2026-10-21T12:00:00+08:00"
    assert out["inputValue"] == "2026-10-21T12:00"
    assert "2026-10-21" in out["currentText"]


def test_frontend_ready_gate():
    out = _run_node("ready_gate")
    assert out["initialSaveDisabled"] is True
    assert out["initialClearDisabled"] is True
    assert out["initialInputDisabled"] is True
    assert out["loadingSaveDisabled"] is True
    assert out["loadingClearDisabled"] is True
    assert out["readySaveDisabled"] is False
    assert out["readyClearDisabled"] is False
    assert out["readyInputDisabled"] is False
    assert out["loadedMode"] is False


def test_frontend_ready_gate_failure():
    out = _run_node("ready_gate_failure")
    assert out["saveDisabled"] is True
    assert out["clearDisabled"] is True
    assert out["inputDisabled"] is True
    assert out["loadedMode"] is None
    assert out["feedback"] != ""


def test_frontend_clear_before_ready_does_not_post():
    out = _run_node("clear_before_ready")
    assert out["postCount"] == 0


def test_frontend_enter_submit():
    out = _run_node("enter_submit")
    assert out["prevented"] is True
    assert out["postCount"] == 1


def test_frontend_switch_back_rejects_stale():
    out = _run_node("switch_back_stale")
    assert out["savedIso"] == "2026-11-06T08:00:00+08:00"
    assert out["inputValue"] == "2026-11-06T08:00"


def test_frontend_mode_switch_clears_draft():
    out = _run_node("mode_switch_clears_draft")
    assert out["inputValue"] == ""
    assert out["dirty"] is False
    assert out["savedIso"] is None
    assert out["loadedMode"] is None


def test_app_wiring():
    app_js = ROOT / "web" / "static" / "js" / "app.js"
    out = _run_node("app_wiring", [str(app_js)])
    assert out["loadedMode"] is False
    assert "2026-10-20" in out["headerRecovery"]
    assert "2026-10-20" in out["formCurrent"]
    assert out["quotaCalls"] >= 1
    assert out["statusCalls"] >= 1
