import ast
from pathlib import Path


def test_ttk_labelframe_does_not_use_textvariable():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    tree = ast.parse(ui_path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "LabelFrame"
            and isinstance(func.value, ast.Name)
            and func.value.id == "ttk"
        ):
            continue
        assert all(keyword.arg != "textvariable" for keyword in node.keywords)


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Combo(dict):
    pass


def test_refresh_scopes_applies_persisted_default_on_fresh_ui(tmp_path):
    from codex_supervisor.store import Store
    from codex_supervisor.ui import SupervisorUI

    project_root = tmp_path / "DEMO_V1"
    (project_root / "Brain").mkdir(parents=True)
    store = Store(tmp_path / "state.db")
    try:
        store.set_project("SBC", str(project_root), make_default=True)
        store.set_scope("SBC", "brain", "Brain", make_default=True)

        ui = object.__new__(SupervisorUI)
        ui.project_var = _Var("SBC")
        ui.scope_var = _Var("")
        ui.target_path_var = _Var("Target: —")
        ui.scope_combo = _Combo()

        SupervisorUI._refresh_scopes(ui, store)

        assert ui.scope_var.get() == "brain"
        assert ui.scope_combo["values"] == ["", "brain"]
        assert ui.target_path_var.get() == f"Target: {(project_root / 'Brain').resolve()}"
    finally:
        store.close()


def test_ui_examples_are_generic_not_project_specific():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert "Project name (example: My Project)" in text
    assert "Scope name (example: Feature Area)" in text
    assert "Project name (example: SBC)" not in text
    assert "Scope name (example: brain)" not in text


def test_split_sequence_lines_creates_one_goal_per_non_empty_line():
    from codex_supervisor.ui import split_sequence_lines

    assert split_sequence_lines("first\n\n second \nthird") == ["first", "second", "third"]


def test_adoption_ui_uses_targeted_search_not_full_history_browser():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert "Find Matching Tasks" in text
    assert "Task title contains" in text
    assert "Scan Full History" not in text


def test_external_owner_controls_are_present():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert "Copy Continue" in text
    assert "Mark Sent" in text
    assert "Mark Complete" in text



def test_v0213_ui_has_effectful_and_retry_controls():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert "Auto-dispatch adopted Desktop tasks" in text
    assert "Retry" in text
    assert "Abandon" in text
    assert "Probe Desktop" in text


def test_timer_message_has_explicit_save_and_multiline_editor():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert 'text="Save Message"' in text
    assert 'text="Continuation message"' in text
    assert 'self.timer_message_entry = tk.Text' in text
    assert 'def _save_timer_message' in text


def test_timer_save_message_only_updates_message_key():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    start = text.index("    def _save_timer_message")
    end = text.index("    def _arm_timer", start)
    body = text[start:end]
    assert 'config["message"] = message' in body
    assert 'config["state"]' not in body
    assert 'config["enabled"]' not in body


def test_timer_ui_separates_advanced_tools():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert 'self.notebook.add(main_tab, text="Timer")' in text
    assert 'self.notebook.add(advanced_tab, text="Labs")' in text
    assert 'Codex Desktop' in text
    assert 'Send Test Now' in text


def test_timer_send_error_callback_captures_exception_text_before_tk_after():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    start = text.index("    def _send_continue_now")
    end = text.index("    def _sync_worker_buttons", start)
    body = text[start:end]
    assert "error_text = str(exc)" in body
    assert "def fail(message: str = error_text)" in body
    assert 'messagebox.showerror("Auto Continue Timer", message' in body


def test_v040_polished_timer_surface_and_presets_are_present():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert 'text="Live status"' in text
    assert 'text="Lifecycle"' in text
    assert 'text="Message preset"' not in text
    assert 'TIMER_PRESETS' in text
    assert '"Finish the task"' in text
    assert '"Verify, then continue"' in text
    assert '"Minimal"' in text


def test_v041_labs_goal_builder_can_preview_and_queue_selected_lines():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert 'text="Goal builder"' in text
    assert 'text="One Goal per line"' in text
    assert 'text="Queue Selected"' in text
    assert 'def _preview_goal_builder' in text
    assert 'def _queue_selected_preview' in text


def test_v041_public_ui_has_creator_credit_and_experimental_labs_notice():
    ui_path = Path(__file__).parents[1] / "codex_supervisor" / "ui.py"
    text = ui_path.read_text(encoding="utf-8")
    assert "Created by Freedomundead" in text
    assert 'text="Labs • experimental"' in text
    assert "Timer mode is the supported product" in text
    assert 'self.desktop_auto_dispatch_var = tk.BooleanVar(value=False)' in text
