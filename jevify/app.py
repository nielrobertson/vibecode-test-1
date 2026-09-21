"""
Jevify — GitHub repo auditor for Jev/TypeSafe opportunities.
Run: python app.py   (then open http://localhost:5000)
"""

import base64
import json
import os
import pathlib

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static")

PRIORITY_FILES = [
    "README.md", "README.rst", "README.txt",
    "package.json", "package-lock.json",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "go.sum",
    "pom.xml", "build.gradle",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "config.py", "config.js", "config.ts",
    "main.py", "app.py", "server.py", "index.py",
    "main.go", "main.rs", "main.js", "main.ts",
    "server.js", "server.ts", "app.js", "app.ts", "index.js", "index.ts",
    "tsconfig.json", ".eslintrc.js", "jest.config.js",
]

SOURCE_EXTENSIONS = [
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".go", ".rs", ".rb", ".java", ".cs",
    ".cpp", ".c", ".h", ".swift", ".kt",
]

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    "vendor", ".next", "coverage", "venv", ".venv", "target",
    ".cache", "out", "tmp", "temp", ".idea", ".vscode",
}

MAX_FILES = 30
MAX_FILE_CHARS = 9_000


def parse_github_url(url: str) -> tuple[str, str]:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "github.com" in url:
        path = url.split("github.com/")[-1]
    else:
        path = url
    parts = path.split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")
    return parts[0], parts[1]


def gh_headers(token: str | None = None) -> dict:
    h = {"Accept": "application/vnd.github.v3+json"}
    token = token or os.getenv("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"token {token}"
    return h


def fetch_file(base_url: str, path: str, headers: dict) -> str | None:
    resp = requests.get(f"{base_url}/contents/{path}", headers=headers, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if isinstance(data, dict) and data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


def should_skip(path: str) -> bool:
    return any(part in SKIP_DIRS for part in path.split("/"))


def fetch_repo_content(owner: str, repo: str, github_token: str | None = None) -> str:
    headers = gh_headers(github_token)
    base_url = f"https://api.github.com/repos/{owner}/{repo}"

    meta = requests.get(base_url, headers=headers, timeout=10)
    meta.raise_for_status()
    info = meta.json()

    default_branch = info.get("default_branch", "main")

    tree_resp = requests.get(
        f"{base_url}/git/trees/{default_branch}?recursive=1",
        headers=headers, timeout=15,
    )
    tree_resp.raise_for_status()
    tree = tree_resp.json()

    all_blobs = [
        item["path"]
        for item in tree.get("tree", [])
        if item["type"] == "blob" and not should_skip(item["path"])
    ]

    parts: list[str] = []
    parts.append(f"# Repository: {owner}/{repo}")
    parts.append(f"Description: {info.get('description') or 'N/A'}")
    parts.append(f"Primary language: {info.get('language') or 'N/A'}")
    parts.append(f"Stars: {info.get('stargazers_count', 0):,}")
    parts.append(f"Open issues: {info.get('open_issues_count', 0):,}")
    parts.append(f"
## File tree ({len(all_blobs)} files shown):
")
    parts.append("
".join(f"  {p}" for p in all_blobs[:300]))

    fetched: set[str] = set()
    count = 0

    def fetch_and_add(path: str):
        nonlocal count
        if path in fetched or count >= MAX_FILES:
            return
        content = fetch_file(base_url, path, headers)
        if content:
            preview = content[:MAX_FILE_CHARS]
            truncated = len(content) > MAX_FILE_CHARS
            suffix = f"
... [truncated — {len(content) - MAX_FILE_CHARS:,} chars omitted]" if truncated else ""
            lang = path.rsplit(".", 1)[-1] if "." in path else ""
            parts.append(f"
## File: {path}
```{lang}
{preview}{suffix}
```")
            fetched.add(path)
            count += 1

    for pf in PRIORITY_FILES:
        candidates = [p for p in all_blobs if p == pf or p.endswith(f"/{pf}")]
        for c in candidates[:1]:
            fetch_and_add(c)

    source = [
        p for p in all_blobs
        if any(p.endswith(ext) for ext in SOURCE_EXTENSIONS) and p not in fetched
    ]
    for sf in source:
        if count >= MAX_FILES:
            break
        fetch_and_add(sf)

    return "
".join(parts)


_PROMPT_FILE = pathlib.Path(__file__).parent / "prompt.txt"
JEVIFY_PROMPT = _PROMPT_FILE.read_text(encoding="utf-8")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/audit", methods=["POST"])
def audit():
    data = request.get_json(silent=True) or {}
    repo_url: str = (data.get("repo_url") or "").strip()
    openai_key: str = (data.get("openai_key") or os.getenv("OPENAI_API_KEY") or "").strip()
    github_token: str | None = (data.get("github_token") or os.getenv("GITHUB_TOKEN") or "").strip() or None
    model: str = (data.get("model") or "gpt-4o").strip()

    if not repo_url:
        return jsonify({"error": "repo_url is required"}), 400
    if not openai_key:
        return jsonify({"error": "OpenAI API key is required"}), 400

    def event(payload: dict) -> str:
        return f"data: {json.dumps(payload)}

"

    def generate():
        try:
            yield event({"type": "status", "message": "Parsing GitHub URL..."})
            try:
                owner, repo = parse_github_url(repo_url)
            except ValueError as exc:
                yield event({"type": "error", "message": str(exc)})
                return
            yield event({"type": "status", "message": f"Fetching {owner}/{repo} from GitHub..."})
            try:
                repo_content = fetch_repo_content(owner, repo, github_token)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response else "?"
                msg = ("Repo not found or private." if status == 404 else f"GitHub API error {status}: {exc}")
                yield event({"type": "error", "message": msg})
                return
            except Exception as exc:
                yield event({"type": "error", "message": f"GitHub error: {exc}"})
                return
            yield event({"type": "status", "message": f"Running Jevify audit with {model}..."})
            try:
                from openai import OpenAI
                client = OpenAI(api_key=openai_key)
                prompt = JEVIFY_PROMPT.format(repo_content=repo_content)
                stream = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4096,
                    stream=True,
                )
                yield event({"type": "start"})
                for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        yield event({"type": "content", "text": delta.content})
                yield event({"type": "done"})
            except ImportError:
                yield event({"type": "error", "message": "openai package not installed"})
            except Exception as exc:
                yield event({"type": "error", "message": f"OpenAI error: {exc}"})
        except Exception as exc:
            yield event({"type": "error", "message": f"Unexpected error: {exc}"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"
Jevify running at http://localhost:{port}
")
    app.run(debug=True, port=port)
