import subprocess
import sys
import json
import re
import time
from pathlib import Path


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import capabilities, exec_safe, gemini, safe_path
from core.safe_path import PathEscape

# A pip requirement that is actually a requirement, not a flag. "-i" reaching
# `pip install` is `--index-url`, which repoints the whole install at a server
# of the attacker's choosing; "-e ." and "--find-links" are the same shape of
# problem. Anchored, so the whole string has to match.
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
                      r"(\[[A-Za-z0-9,._-]{1,64}\])?"
                      r"((==|>=|<=|~=|!=|>|<)[A-Za-z0-9._*+-]{1,32})?$")


def _safe_requirement(raw: str) -> str:
    """Return `raw` if it is a plain requirement, else ''."""
    text = str(raw or "").strip()
    return text if _SPEC_RE.match(text) else ""


def _safe_project_file(project_dir: Path, relative: str) -> Path:
    """Where a planned file may be written.

    `relative` comes out of the model's JSON plan, so it is exactly as
    trustworthy as the rest of the plan — which is to say, not. The planner
    prompt asks for relative paths; this is what makes that a rule rather than
    a request. `{"path": "../../.bashrc"}` raises instead of being written."""
    return safe_path.resolve_within(project_dir, relative, allow_absolute=False)

MODEL_PLANNER    = gemini.SMART
MODEL_WRITER     = gemini.SMART

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_model(model_name: str = gemini.SMART):
    """Planning and writing whole files — the reasoning tier, and a long
    deadline because the answer is a source file rather than a sentence."""
    class _W:
        def generate_content(self, contents):
            resp = gemini.call(contents, tier=model_name, timeout_ms=60000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:

    low = output.lower()

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


def _has_error(output: str, run_command: str) -> bool:
    
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    error_type = _classify_error(output)
    return error_type != "none"

class RateLimitError(Exception):
    pass


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        full_path = _safe_project_file(project_dir, file_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    """Install what the plan asked for — and say plainly when it did not work.

    The old version had no timeout on `pip show`, and reported a failed install
    as "Install warning (non-fatal)" before carrying on to report the project
    as built. A missing dependency is not non-fatal; it is the reason the next
    step fails."""
    if not dependencies:
        return "No external dependencies."

    requested, rejected = [], []
    for dep in dependencies:
        clean = _safe_requirement(dep)
        (requested if clean else rejected).append(clean or str(dep)[:40])

    to_install = []
    for spec in requested:
        pkg_name = re.split(r"[>=<!~\[]", spec)[0].strip()
        probe = exec_safe.run(
            [exec_safe.python_executable(), "-m", "pip", "show", pkg_name],
            timeout=30,
        )
        if not probe.ok:
            to_install.append(spec)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    notes = []
    if rejected:
        notes.append(f"Refused {len(rejected)} dependency name(s) that did not "
                     f"look like plain package names: {', '.join(rejected[:3])}")

    if not to_install:
        return " ".join(notes) or f"All dependencies already installed."

    print(f"[DevAgent] 📦 Installing: {to_install}")
    result = exec_safe.run(
        [exec_safe.python_executable(), "-m", "pip", "install",
         "--disable-pip-version-check", *to_install],
        timeout=300, cwd=str(project_dir),
    )
    if result.timed_out:
        notes.append(f"Installing {', '.join(to_install)} timed out after five "
                     f"minutes and was stopped.")
    elif not result.ok:
        notes.append(f"Could NOT install {', '.join(to_install)}: "
                     f"{result.failure_detail(200)}")
    else:
        notes.append(f"Installed: {', '.join(to_install)}")
    return " ".join(notes)


def _open_vscode(project_dir: Path) -> bool:
    """Open the finished project in an editor, if one is installed.

    The old version passed a list with `shell=True`, which on POSIX runs only
    `code` and silently throws the project path away, and on Windows hands the
    whole thing to cmd. Best effort either way — a missing editor is not a
    failure of the build."""
    for name in ("code", "code-insiders", "codium"):
        path = exec_safe.resolve_program(name)
        if not path:
            continue
        started, _detail = exec_safe.spawn([path, str(project_dir)])
        if started:
            time.sleep(1.0)
            print(f"[DevAgent] 💻 Editor opened: {project_dir}")
            return True
    return False


def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    """Run the generated project's entry point.

    `run_command` comes out of the model's plan. It is split on whitespace and
    never sees a shell, so there is no metacharacter to exploit — but it is
    still an arbitrary program name, which is why the whole tool sits behind
    `code.execute` and a human. The working directory is the project folder,
    and the process group is killed on timeout rather than orphaned."""
    print(f"[DevAgent] 🚀 Running: {run_command}")
    parts = str(run_command or "").split()
    if not parts:
        return "The plan did not say how to run the project."

    if parts[0].lower() in ("python", "python3", "py"):
        parts[0] = exec_safe.python_executable()

    result = exec_safe.run(parts, timeout=max(1, int(timeout)),
                           cwd=str(project_dir))

    if result.timed_out:
        return (f"Timed out after {timeout}s — a long-running app (server or "
                f"GUI) is likely working; I stopped it to carry on.")
    if result.error:
        return f"Command not found or could not start: {result.summary()}"

    chunks = []
    if result.stdout.strip():
        chunks.append(f"STDOUT:\n{result.stdout.strip()}")
    if result.stderr.strip():
        chunks.append(f"STDERR:\n{result.stderr.strip()}")
    if result.returncode:
        chunks.append(f"EXIT CODE: {result.returncode}")
    return "\n\n".join(chunks) if chunks else "Ran with no output."


def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    """Install the package a ModuleNotFoundError named, if it names one safely.

    The package name is taken from a traceback, which is program output rather
    than a trusted source, so it has to match a plain package name. Without
    that check a crafted error message saying `No module named '-i'` would put
    `-i` on a pip command line, and `-i` is `--index-url`."""
    match = re.search(r"No module named ['\"]([A-Za-z0-9_.\-]{1,64})['\"]",
                      error_output, re.IGNORECASE)
    if not match:
        return False

    pkg = match.group(1).split(".")[0].replace("_", "-")
    if not _PKG_RE.match(pkg):
        print(f"[DevAgent] refusing to install suspicious package name: {pkg!r}")
        return False

    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg}")
    result = exec_safe.run(
        [exec_safe.python_executable(), "-m", "pip", "install",
         "--disable-pip-version-check", pkg],
        timeout=180, cwd=str(project_dir),
    )
    return result.ok


def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            full_path = _safe_project_file(project_dir, fix_path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except PathEscape:
            print(f"[DevAgent] ⚠️ Refused to write {fix_path}: outside the "
                  f"project folder.")
        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:

    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir  = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except PathEscape:
                log(f"REFUSED {file_path}: the plan asked for a file outside "
                    f"the project folder. Skipping it.")
                break
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    install_note = ""
    if dependencies:
        install_note = _install_dependencies(dependencies, project_dir)
        log(install_note)
        if "Could NOT install" in install_note or "timed out" in install_note:
            # Carried forward into the final message instead of being dropped:
            # the build very likely fails next, and this is why.
            install_note = f"\n\nNote: {install_note}"
        else:
            install_note = ""

    _open_vscode(project_dir)

    last_output   = ""
    auto_installs = 0  

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        last_output = _run_project(run_command, project_dir, timeout)
        log(f"Output preview: {last_output[:150]}")

        if not _has_error(last_output, run_command):
            msg = (
                f"Project '{proj_name}' is working, sir. "
                f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to: {project_dir}"
            )
            if speak: speak(msg)
            return f"{msg}{install_note}\n\nOutput:\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(last_output)
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        log(f"Fixing errors (type: {error_type})...")
        try:
            updated = _fix_files(
                error_output=last_output,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    return f"{msg}{install_note}\n\nLast error:\n{last_output[:600]}"


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))

    if not description:
        return "Please describe the project you want me to build, sir."

    return _build_project(
        description  = description,
        language     = language,
        project_name = project_name,
        timeout      = timeout,
        speak        = speak,
        player       = player,
    )


# ── Capability ───────────────────────────────────────────────────────────────
#
# This tool writes files, installs packages from the internet and runs code it
# just generated. Three consequential things, and the honest way to present
# that is one confirmation that names all three — not three prompts in a row,
# and certainly not none, which is what it had.

def _dev_guard(params: dict) -> dict:
    description = str((params or {}).get("description", "")).strip()[:120]
    name        = str((params or {}).get("project_name", "")).strip()
    return {
        "summary": f"Build a project{f' called {name}' if name else ''}",
        "detail": (f"I will write Python for \"{description}\", install any "
                   f"packages it needs with pip, and run it — with your full "
                   f"user account, in {PROJECTS_DIR.name}."),
        "target": str(PROJECTS_DIR / (name or "")),
    }


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "dev_agent",
    "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "description": {
                "type": "STRING",
                "description": "What the project should do"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "project_name": {
                "type": "STRING",
                "description": "Optional project folder name"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Run timeout in seconds (default: 30)"
            }
        },
        "required": [
            "description"
        ]
    },
    "handler": dev_agent,
    "capability": capabilities.CODE_EXEC,
    "guard": _dev_guard,
}
