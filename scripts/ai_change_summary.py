import os
import json
import time
import base64
import textwrap
from typing import List, Dict, Tuple

import requests

GITHUB_API = "https://api.github.com"
OPENAI_API = "https://api.openai.com/v1/chat/completions"
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_PATCH_CHARS = int(os.getenv("MAX_PATCH_CHARS", "12000"))  # 대형 PR 대비 청킹
CHUNK_OVERLAP = 500


def _gh(headers: Dict, method: str, url: str, **kwargs):
    r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
    return r


def _openai(headers: Dict, messages: List[Dict]) -> str:
    r = requests.post(
        OPENAI_API,
        headers=headers,
        json={
            "model": MODEL,
            "messages": messages,
            "temperature": 0.2,
        },
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text}")
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def load_prompt() -> str:
    path = os.path.join(os.getcwd(), "prompts", "change_summary_prompt.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    # fallback
    return (
        "You are a senior software engineer. Given a diff patch, produce a precise before/after change summary."
    )


def get_event() -> Dict:
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        raise RuntimeError("Missing GITHUB_EVENT_PATH")
    with open(event_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_pr_info(event: Dict) -> Tuple[str, int]:
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("Missing GITHUB_REPOSITORY")
    pr_number = event.get("number") or event.get("pull_request", {}).get("number")
    if not pr_number:
        raise RuntimeError("Not a pull_request event")
    return repo, int(pr_number)


def fetch_changed_files(headers: Dict, repo: str, pr_number: int) -> List[Dict]:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files?per_page=100"
    files = []
    while url:
        resp = _gh(headers, "GET", url)
        files.extend(resp.json())
        # pagination
        link = resp.headers.get("Link", "")
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part[part.find("<") + 1 : part.find(">")]
        url = next_url
    return files


def chunk_text(s: str, size: int = MAX_PATCH_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    if len(s) <= size:
        return [s]
    chunks = []
    start = 0
    n = len(s)
    while start < n:
        end = min(start + size, n)
        chunks.append(s[start:end])
        if end == n:
            break
        start = max(0, end - overlap)
    return chunks


def build_file_patch(files: List[Dict]) -> str:
    parts = []
    for f in files:
        filename = f.get("filename")
        status = f.get("status")  # modified/added/removed/renamed
        patch = f.get("patch") or ""
        # patch가 없을 수 있음(바이너리/대용량). 그 경우 헤더만 포함
        header = f"\n==== FILE: {filename} (status: {status}) ===="
        if not patch:
            parts.append(header + "\n(no textual patch — binary or large file; summarize by filename only)\n")
            continue
        parts.append(header + "\n" + patch + "\n")
    return "\n".join(parts)


def make_summary(openai_key: str, patch_text: str) -> str:
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }
    sys_prompt = load_prompt()
    chunks = chunk_text(patch_text)

    summaries = []
    for i, ch in enumerate(chunks, 1):
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"[Chunk {i}/{len(chunks)}]\n\n{ch}"},
        ]
        part = _openai(headers, messages)
        summaries.append(f"### Chunk {i}/{len(chunks)}\n" + part)
        # API rate-limit 여유
        time.sleep(0.5)

    if len(summaries) == 1:
        return summaries[0]

    # 최종 머지 요약
    merge_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Combine the following chunked summaries into one concise PR review summary with the same sections.\n\n" + "\n\n".join(summaries)},
    ]
    final = _openai(headers, merge_messages)
    return final


def post_pr_comment(headers: Dict, repo: str, pr_number: int, body: str):
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    payload = {"body": body}
    _gh(headers, "POST", url, json=payload)


# ... (상단 동일)
def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI change summarizer")
    parser.add_argument("--base", type=str)
    parser.add_argument("--head", type=str)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--out", type=str, default="")   # ← 추가/유지
    args = parser.parse_args()
    # ... (중략)

    # --- Mode 1: manual diff ---
    if args.base and args.head:
        patch = run_git_diff(args.base, args.head)
        if not patch.strip():
            print("No textual diff between the specified range.")
            return
        summary = summarize_patch(openai_key, patch)
        output = f"## 🤖 AI Change Summary (manual diff {args.base}..{args.head})\n\n{summary}\n"
        if args.out:
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Wrote summary to {args.out}")
        else:
            print(output)
        return

    # repo/env 공통
    repo = os.getenv("GITHUB_REPOSITORY") or ""
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY not set")

    # --- Mode 2: existing PR by number ---
    if args.pr:
        files = fetch_changed_files(gh_headers, repo, args.pr)
        patch = build_file_patch(files)
        summary = summarize_patch(openai_key, patch)
        output = f"## 🤖 AI Change Summary for PR #{args.pr}\n\n{summary}\n"
        # 코멘트
        if os.getenv("CI"):
            post_pr_comment(gh_headers, repo, args.pr, output + "\n— Generated by AI Change Summarizer")
        # 파일 저장
        if args.out:
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(output)
        else:
            print(output)
        return

    # --- Mode 3: default PR event ---
    event = get_event()
    pr_number = event.get("number") or event.get("pull_request", {}).get("number")
    if not pr_number:
        raise RuntimeError("Not a pull_request event and no manual args provided")
    pr_number = int(pr_number)

    files = fetch_changed_files(gh_headers, repo, pr_number)
    patch = build_file_patch(files)
    summary = summarize_patch(openai_key, patch)
    output = f"## 🤖 AI Change Summary for PR #{pr_number}\n\n{summary}\n"

    # 코멘트
    post_pr_comment(gh_headers, repo, pr_number, output + "\n\n— Generated by AI Change Summarizer")

    # 파일 저장(추가)
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote summary to {args.out}")


if __name__ == "__main__":
    main()
