#!/usr/bin/env python3
"""Extract latest reports from Claude Cowork scheduled task sessions."""

import json
import os
import glob
from datetime import datetime

SESSIONS_DIR = os.path.expanduser(
    "~/Library/Application Support/Claude/local-agent-mode-sessions/"
    "fa3ec640-aa5b-4693-9164-a2ba27fd1171/"
    "62a5e0e8-fde4-4b80-9461-cd3f05718693"
)
SCHEDULED_TASKS_FILE = os.path.join(SESSIONS_DIR, "scheduled-tasks.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "reports-data.json")
USAGE_FILE = os.path.join(SCRIPT_DIR, "usage-data.json")

# Task ID -> keywords to match in session titles OR initial messages
TASK_KEYWORDS = {
    "weekly-employee-analysis": ["weekly employee analysis", "employee analysis"],
    "alpaca-competitor-check": ["alpaca competitor", "alpaca"],
    "weekly-employee-performance-analysis": ["employee performance", "performance analysis"],
    "10xseo-weekly-monitor": ["10xseo weekly monitor", "10xseo weekly", "weekly monitor"],
    "10xseo-biweekly-analytics": ["10xseo biweekly", "biweekly analytics", "analyze website analytics and seo", "hotjar.*search console"],
}

# Keywords to also match in initialMessage (for tasks whose titles don't match well)
TASK_INIT_KEYWORDS = {
    "10xseo-biweekly-analytics": ["hotjar", "search console.*google analytics", "10xseo.*analytics"],
}

# Negative keywords — sessions with these titles are NOT task reports
EXCLUDE_TITLE_PATTERNS = [
    r"ახალი დიზაინი",
    r"new design",
    r"command.?cent",
    r"დაშედულებული",
    r"შეცვალე",
    r"გამოასწორე",
    r"debug",
]

def load_scheduled_tasks():
    with open(SCHEDULED_TASKS_FILE) as f:
        data = json.load(f)
    return {t["id"]: t for t in data["scheduledTasks"] if t["id"] in TASK_KEYWORDS}

def find_sessions_for_task(task_id, keywords):
    """Find all sessions matching a task by title or initialMessage keywords."""
    import re
    matches = []
    init_keywords = TASK_INIT_KEYWORDS.get(task_id, [])

    for f in glob.glob(os.path.join(SESSIONS_DIR, "local_*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
            title = (data.get("title") or "").lower()
            init_msg = str(data.get("initialMessage") or "").lower()[:500]
            found = False

            # Check title keywords
            for kw in keywords:
                if re.search(kw, title):
                    found = True
                    break

            # Check initialMessage keywords
            if not found:
                for kw in init_keywords:
                    if re.search(kw, init_msg):
                        found = True
                        break

            # Exclude false positives
            if found:
                excluded = False
                for exc_pat in EXCLUDE_TITLE_PATTERNS:
                    if re.search(exc_pat, title):
                        excluded = True
                        break
                if not excluded:
                    matches.append({
                        "session_id": data["sessionId"],
                        "title": data.get("title", ""),
                        "created": data.get("createdAt", 0),
                        "last_activity": data.get("lastActivityAt", 0),
                    })
        except Exception:
            pass
    # Sort by creation time, newest first
    matches.sort(key=lambda x: x["created"], reverse=True)
    return matches

def is_meta_message(text):
    """Check if a message is a meta/confirmation message rather than an actual report."""
    import re
    lower = text.lower().strip()
    # Short messages that are just confirmations
    meta_patterns = [
        r"^(თასქი|ანგარიში|რეპორტი).{0,30}(განახლდა|გაიგზავნა|დასრულდა|შეიქმნა)",
        r"^(დაყენდა|გაგზავნილია|შესრულდა|მზადაა)",
        r"^(✅|🎯|📊|📈|✨).{0,50}$",
        r"^(sent|done|completed|updated|finished)",
        r"chrome.*connect",
        r"browser.*error",
        r"i('ll| will) (now |)send",
        r"^(ყოველკვირეული|ანალიზი).{0,60}(დასრულდა|გაიგზავნა)",
        r"let me send",
        r"report has been sent",
        r"message.*sent.*successfully",
        r"^შეიქმნა დაგეგმილი",
        r"i found instructions",
    ]
    for pat in meta_patterns:
        if re.search(pat, lower):
            return True
    # Very short messages are likely meta
    if len(text) < 300:
        return True
    return False


def is_not_report_content(text):
    """Check if text is code/config/SKILL.md rather than an actual report."""
    stripped = text.strip()
    # SKILL.md frontmatter
    if stripped.startswith("---") and "name:" in stripped[:200]:
        return True
    # JSON config
    if stripped.startswith("{") and ('"taskName"' in stripped[:200] or '"cronExpression"' in stripped[:200]):
        return True
    # JavaScript/code
    if stripped.startswith("const ") or stripped.startswith("import ") or stripped.startswith("function "):
        return True
    if stripped.startswith("#!/"):
        return True
    return False

def extract_report_from_session(session_id):
    """Extract the actual report from a session's JSONL.

    Priority:
    1. Content written via Write tool to output/report files
    2. Slack message content (the actual sent report)
    3. Longest non-meta assistant text message
    """
    session_dir = os.path.join(SESSIONS_DIR, session_id)
    jsonl_files = glob.glob(os.path.join(session_dir, ".claude", "projects", "*", "*.jsonl"))

    if not jsonl_files:
        return None

    # Use the main JSONL (not subagent ones)
    main_jsonl = [f for f in jsonl_files if "subagent" not in f]
    if not main_jsonl:
        return None

    jsonl_file = main_jsonl[0]

    try:
        with open(jsonl_file) as f:
            lines = f.readlines()
    except Exception:
        return None

    # Collect reports from different sources
    written_reports = []  # From Write tool (file outputs)
    slack_reports = []    # From slack_send_message
    assistant_texts = []  # Plain assistant text

    for line in lines:
        try:
            msg = json.loads(line.strip())
            if msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content", [])
                text_parts = []
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            text_parts.append(c["text"])
                        elif c.get("type") == "tool_use":
                            name = c.get("name", "")
                            inp = c.get("input", {})
                            # Extract from Write tool (report files)
                            if name == "Write":
                                fp = inp.get("file_path", "").lower()
                                ct = inp.get("content", "")
                                if len(ct) > 300 and any(kw in fp for kw in ["report", "output", "analysis", "ანალიზ"]):
                                    written_reports.append(ct)
                            # Extract from Slack messages
                            elif "slack_send_message" in name:
                                msg_text = inp.get("message", "")
                                if len(msg_text) > 200:
                                    slack_reports.append(msg_text)
                if text_parts:
                    full_text = "\n".join(text_parts)
                    if len(full_text) > 100:
                        assistant_texts.append(full_text)
        except Exception:
            pass

    # Priority 1: Written report files (most complete, filter out code/config)
    if written_reports:
        real_written = [r for r in written_reports if not is_not_report_content(r)]
        if real_written:
            return max(real_written, key=len)

    # Priority 2: Slack messages (the actual sent report)
    if slack_reports:
        real_slack = [r for r in slack_reports if not is_not_report_content(r)]
        if real_slack:
            if len(real_slack) == 1:
                return real_slack[0]
            combined = "\n\n---\n\n".join(real_slack)
            return combined

    # Priority 3: Longest non-meta assistant text
    if assistant_texts:
        real_reports = [t for t in assistant_texts
                        if not is_meta_message(t) and not is_not_report_content(t)]
        if real_reports:
            return max(real_reports, key=len)

    return None

def main():
    tasks = load_scheduled_tasks()
    reports = {}

    for task_id, task_data in tasks.items():
        keywords = TASK_KEYWORDS.get(task_id, [])
        sessions = find_sessions_for_task(task_id, keywords)

        task_report = {
            "task_id": task_id,
            "cron": task_data.get("cronExpression", ""),
            "enabled": task_data.get("enabled", False),
            "last_run_at": task_data.get("lastRunAt", ""),
            "runs": []
        }

        # Get reports from last 3 sessions
        for session in sessions[:3]:
            report_text = extract_report_from_session(session["session_id"])
            if report_text:
                created_dt = datetime.fromtimestamp(session["created"] / 1000).isoformat()
                task_report["runs"].append({
                    "session_id": session["session_id"],
                    "title": session["title"],
                    "date": created_dt,
                    "report": report_text[:10000],  # Limit size
                })

        reports[task_id] = task_report

    # Load usage data if available
    usage = None
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE) as f:
                usage = json.load(f)
        except Exception:
            pass

    # Write output
    output = {
        "generated_at": datetime.now().isoformat(),
        "tasks": reports,
    }
    if usage:
        output["usage"] = usage

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Extracted reports for {len(reports)} tasks -> {OUTPUT_FILE}")
    for tid, tr in reports.items():
        print(f"  {tid}: {len(tr['runs'])} runs found")

    # Auto-push to GitHub if in a git repo
    git_push()

def git_push():
    """Commit and push reports-data.json to GitHub."""
    import subprocess
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        # Check if there are changes
        result = subprocess.run(
            ["git", "diff", "--quiet", "reports-data.json"],
            cwd=repo_dir, capture_output=True
        )
        if result.returncode == 0:
            print("No changes to push.")
            return

        subprocess.run(
            ["git", "add", "reports-data.json"],
            cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"Update reports {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=repo_dir, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "push"],
            cwd=repo_dir, check=True, capture_output=True,
            timeout=30
        )
        print("Pushed to GitHub.")
    except Exception as e:
        print(f"Git push failed: {e}")

if __name__ == "__main__":
    main()
